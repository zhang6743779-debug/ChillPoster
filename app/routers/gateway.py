import httpx
import json
import asyncio
import re
import hashlib
import websockets
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse
from starlette.requests import ClientDisconnect
from cachetools import TTLCache
from app.routers.config_302 import get_config_302
from core.logger import logger
from core.media_library_cache import get_item_by_pickcode
from app.services.drive115_service import drive115_service
from app.services.wechat_service import wechat_notify_service

router = APIRouter(tags=["gateway"])

# ==========================================================
# 端口到 Emby 索引的映射 (启动时填充)
# ==========================================================
PORT_TO_EMBY_INDEX = {}

def register_gateway_port(port: int, emby_index: int):
    """注册网关端口与 Emby 索引的映射关系"""
    PORT_TO_EMBY_INDEX[port] = emby_index

async def get_emby_config_by_port(request_port: int):
    """根据请求端口获取对应的 Emby 配置"""
    cfg = await get_config_302()
    embys = cfg.get("embys", [])

    if not isinstance(embys, list) or len(embys) == 0:
        return {}, -1

    # 根据端口找到对应的 Emby 索引
    emby_index = PORT_TO_EMBY_INDEX.get(request_port, 0)

    # 确保索引有效
    if emby_index >= len(embys):
        emby_index = 0

    target_emby = embys[emby_index]

    if not target_emby.get("enabled"):
        # 如果指定的 Emby 未启用，找第一个启用的
        for i, e in enumerate(embys):
            if e.get("enabled"):
                target_emby = e
                emby_index = i
                break

    if hasattr(target_emby, 'dict'):
        target_emby = target_emby.dict()

    return target_emby, emby_index
preload_dedupe_cache = TTLCache(maxsize=1000, ttl=10)   # 预加载去重
playback_notify_cache = TTLCache(maxsize=1000, ttl=30)  # 播放通知去重（30秒内同一 item 不重复通知）
strm_redirect_cache = TTLCache(maxsize=2000, ttl=2)     # STRM 302 去重（短时间内同一 pickcode 只记一次日志）
name_cache = TTLCache(maxsize=10000, ttl=86400)         # ID -> 片名 映射表

# ==========================================================
# 全局 HTTP 客户端
# ==========================================================
proxy_client = httpx.AsyncClient(
    timeout=60.0, 
    follow_redirects=True, 
    verify=False,
    limits=httpx.Limits(max_keepalive_connections=200, max_connections=500)
)

async def get_emby_config():
    """获取默认的 Emby 配置（兼容旧代码）"""
    cfg = await get_config_302()
    embys = cfg.get("embys", [])
    target_emby = None

    if isinstance(embys, list) and len(embys) > 0:
        target_emby = next((e for e in embys if e.get('enabled')), None)
        if not target_emby:
            target_emby = embys[0]

    if not target_emby:
        target_emby = {}

    if hasattr(target_emby, 'dict'):
        target_emby = target_emby.dict()

    base_url = target_emby.get("url", "").rstrip("/")
    api_key = target_emby.get("key", "")

    return base_url, api_key, target_emby


# ==================================================================
# [辅助] 生成人性化的标题
# ==================================================================
def get_friendly_name(item_data: dict) -> str:
    """从 Emby 数据中提取 中文标题/剧集号"""
    try:
        name = item_data.get("Name", "未知标题")
        item_type = item_data.get("Type")
        year = item_data.get("ProductionYear", "")

        if item_type == "Episode":
            series_name = item_data.get("SeriesName", "")
            season_idx = item_data.get("ParentIndexNumber", "?")
            episode_idx = item_data.get("IndexNumber", "?")
            return f"📺 {series_name} S{season_idx}E{episode_idx} - {name}"
        elif item_type == "Movie":
            return f"🎬 {name} ({year})"
        else:
            return f"{name}"
    except:
        return item_data.get("Name", "Unknown")


def _format_emby_item_info(item_info: dict, item_id: str) -> dict:
    item_type = item_info.get("type", "")
    original_title = item_info.get("original_title", "")
    media_type = "tv" if item_type in ("Episode", "Series") else "movie"

    if item_type == "Episode":
        series_name = item_info.get("series_name", "")
        display_name = f"📺 {series_name} S{item_info.get('season', '?')}E{item_info.get('episode', '?')} - {item_info.get('name', '')}"
        original_name = original_title or series_name
    elif item_type == "Movie":
        year_str = f" ({item_info['year']})" if item_info.get("year") else ""
        display_name = f"🎬 {item_info.get('name', '')}{year_str}"
        original_name = original_title or item_info.get("name", "")
    elif item_type == "Series":
        year_str = f" ({item_info['year']})" if item_info.get("year") else ""
        display_name = f"📺 {item_info.get('name', '')}{year_str}"
        original_name = original_title or item_info.get("name", "")
    else:
        display_name = item_info.get("name", f"ID: {item_id}")
        original_name = original_title or item_info.get("name", "")

    cr = item_info.get("community_rating")
    return {
        "display_name": display_name,
        "original_name": original_name,
        "media_type": media_type,
        "poster_url": item_info.get("poster_url") or "",
        "overview": item_info.get("overview", "") or "",
        "rating": str(round(cr, 1)) if cr else "",
        "genres": item_info.get("genres", "") or "",
        "tagline": item_info.get("tagline", "") or "",
        "year": str(item_info.get("year", "")) if item_info.get("year") else "",
        "tmdb_id": item_info.get("tmdb_id", ""),
        "full_meta": True,
    }


async def _resolve_preload_name(item_id: str, emby_cfg: dict) -> str:
    """为预缓存日志解析可读标题，缓存未命中时补查 Emby。"""
    cached = name_cache.get(item_id)
    if isinstance(cached, dict):
        display_name = cached.get("display_name", "")
        if display_name and not display_name.startswith("ID: "):
            return display_name
    elif isinstance(cached, str) and cached and not cached.startswith("ID: "):
        return cached

    try:
        from core.emby_client import EmbyClient
        emby_client = EmbyClient(
            host=emby_cfg.get("url", ""),
            key=emby_cfg.get("key", ""),
            public_host=emby_cfg.get("public_host")
        )
        item_info = await asyncio.to_thread(emby_client.get_item_info, item_id)
        if item_info:
            meta = _format_emby_item_info(item_info, item_id)
            name_cache[item_id] = meta
            display_name = meta.get("display_name", "")
            if display_name:
                return display_name
    except Exception as e:
        logger.debug(f"[预缓存] 获取媒体标题失败: {e}")

    return f"ID: {item_id}"


def _derive_media_display_name_from_path(path: str) -> str:
    parts = [p for p in str(path or "").strip().strip("/").split("/") if p]
    if len(parts) < 2:
        return ""

    file_name = parts[-1]
    stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    parent_name = parts[-2]
    if re.search(r"(?i)\.(mkv|mp4|ts|m2ts|avi|mov|wmv|flv|webm|strm)$", file_name):
        if parent_name in {"转存目录", "下载目录", "下载库", "整理目录"} or "转存" in parent_name:
            return stem
    season_idx = next((i for i, p in enumerate(parts) if re.match(r"(?i)^season\s*\d+$", p)), None)
    if season_idx is not None and season_idx >= 1:
        series_name = parts[season_idx - 1]
        season_name = parts[season_idx]
        episode_match = re.search(r"(?i)\bS\d{1,2}E\d{1,4}\b", stem)
        episode_name = episode_match.group(0).upper() if episode_match else ""
        return " / ".join(p for p in [series_name, season_name, episode_name] if p)

    return parent_name


def _resolve_direct_pickcode_name(pickcode: str, fallback_name: str) -> str:
    try:
        cached = get_item_by_pickcode(pickcode)
        item = cached.get("item", {}) if isinstance(cached, dict) else {}
        path = str(item.get("path", "") or "").strip()
        display_name = _derive_media_display_name_from_path(path)
        if display_name:
            return display_name
        name = str(item.get("name", "") or "").strip()
        if name:
            return name
        if path:
            tail = path.rstrip("/").rsplit("/", 1)[-1]
            if tail:
                return tail
    except Exception as e:
        logger.debug(f"[Gateway] 按 pickcode 查询媒体库缓存失败: {e}")
    return fallback_name


def _build_redirect_response(direct_url: str):
    resp = RedirectResponse(url=direct_url, status_code=302)
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _format_proxy_error(error: Exception) -> str:
    error_type = type(error).__name__
    error_text = str(error).strip() or repr(error)
    return f"type={error_type} error={error_text}"


def _parse_emby_authorization(header: str) -> dict:
    parsed = {}
    for part in str(header or "").replace(",", " ").split():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        parsed[key.strip().lower()] = value.strip('"').strip("'")
    return parsed


def _get_session_user_name(session: dict) -> str:
    user_name = str(session.get("UserName", "") or "").strip()
    if user_name:
        return user_name
    user = session.get("User")
    if isinstance(user, dict):
        return str(user.get("Name", "") or user.get("UserName", "") or "").strip()
    return ""


def _session_matches_token(session: dict, auth_token: str) -> bool:
    if not auth_token:
        return False
    candidates = [
        session.get("AccessToken"),
        session.get("Token"),
        session.get("ApiKey"),
    ]
    return any(str(candidate or "") == auth_token for candidate in candidates)


def _session_matches_client(session: dict, auth_client: str, auth_device: str, user_agent: str) -> bool:
    session_client = str(session.get("Client", "") or "").strip().lower()
    session_device = str(session.get("DeviceName", "") or "").strip().lower()
    auth_client_l = str(auth_client or "").strip().lower()
    auth_device_l = str(auth_device or "").strip().lower()
    if auth_client_l and session_client and auth_client_l == session_client:
        if not auth_device_l or auth_device_l == session_device:
            return True
    if auth_device_l and session_device and auth_device_l == session_device:
        return True
    ua = str(user_agent or "").lower()
    return bool(ua and session_client and session_client in ua)


def _session_matches_identity(session: dict, auth_token: str, auth_client: str, auth_device: str) -> bool:
    if _session_matches_token(session, auth_token):
        return True

    auth_client_l = str(auth_client or "").strip().lower()
    auth_device_l = str(auth_device or "").strip().lower()
    if not auth_device_l:
        return False

    session_client = str(session.get("Client", "") or "").strip().lower()
    session_device = str(session.get("DeviceName", "") or "").strip().lower()
    if session_device != auth_device_l:
        return False
    return not auth_client_l or not session_client or auth_client_l == session_client


def _session_user_key(session: dict) -> str:
    value = str(session.get("UserId", "") or "").strip()
    if value:
        return f"user:{value}"
    user_name = _get_session_user_name(session)
    if user_name:
        return f"name:{user_name}"
    return ""


def _fallback_user_key(auth_token: str, auth_client: str, auth_device: str, user_agent: str) -> str:
    if auth_token:
        digest = hashlib.sha1(auth_token.encode("utf-8", "ignore")).hexdigest()[:16]
        return f"token:{digest}"
    client_bits = "|".join(str(item or "").strip() for item in (auth_client, auth_device, user_agent[:80]))
    if client_bits.strip("|"):
        digest = hashlib.sha1(client_bits.encode("utf-8", "ignore")).hexdigest()[:16]
        return f"client:{digest}"
    return ""


async def _fetch_emby_sessions(base_url: str, api_key: str) -> list:
    if not base_url or not api_key:
        return []
    try:
        resp = await proxy_client.get(
            f"{base_url}/emby/Sessions",
            headers={"X-Emby-Token": api_key},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json() or []
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug(f"[Gateway] 获取 Emby Sessions 失败: {type(e).__name__} {repr(e)}")
    return []


def _match_request_session(
    sessions: list,
    item_id: str,
    auth_token: str,
    auth_client: str,
    auth_device: str,
    user_agent: str,
    *,
    allow_item_fallback: bool = True,
) -> dict | None:
    if item_id:
        for sess in sessions:
            if str(sess.get("NowPlayingItem", {}).get("Id", "") or "") == str(item_id):
                if _session_matches_token(sess, auth_token) or _session_matches_client(sess, auth_client, auth_device, user_agent):
                    return sess
        if allow_item_fallback:
            for sess in sessions:
                if str(sess.get("NowPlayingItem", {}).get("Id", "") or "") == str(item_id):
                    return sess
    for sess in sessions:
        if _session_matches_token(sess, auth_token):
            return sess
    for sess in sessions:
        if _session_matches_client(sess, auth_client, auth_device, user_agent):
            return sess
    return None


def _match_request_identity_session(sessions: list, item_id: str, auth_token: str, auth_client: str, auth_device: str) -> dict | None:
    if item_id:
        for sess in sessions:
            if str(sess.get("NowPlayingItem", {}).get("Id", "") or "") == str(item_id):
                if _session_matches_identity(sess, auth_token, auth_client, auth_device):
                    return sess
    for sess in sessions:
        if _session_matches_identity(sess, auth_token, auth_client, auth_device):
            return sess
    return None


async def _build_rapid_context(base_url: str, api_key: str, item_id: str, auth_token: str, auth_client: str, auth_device: str, user_agent: str) -> tuple[dict, list, dict | None]:
    sessions = await _fetch_emby_sessions(base_url, api_key)
    identity_session = _match_request_identity_session(
        sessions,
        item_id,
        auth_token,
        auth_client,
        auth_device,
    )
    matched_session = identity_session or _match_request_session(
        sessions,
        item_id,
        auth_token,
        auth_client,
        auth_device,
        user_agent,
        allow_item_fallback=True,
    )
    user_key = _session_user_key(identity_session) if identity_session else ""
    user_name = _get_session_user_name(identity_session) if identity_session else ""
    if not user_key:
        user_key = _fallback_user_key(auth_token, auth_client, auth_device, user_agent)

    active_user_keys = []
    active_item_ids = []
    active_playbacks = []
    for sess in sessions:
        if not isinstance(sess, dict) or not sess.get("NowPlayingItem"):
            continue
        now_playing = sess.get("NowPlayingItem") or {}
        active_item_id = str(now_playing.get("Id", "") or "").strip()
        if active_item_id:
            active_item_ids.append(active_item_id)
        active_key = _session_user_key(sess)
        if active_key:
            active_user_keys.append(active_key)
        active_playbacks.append({
            "session_id": str(sess.get("Id") or "").strip(),
            "user_key": active_key,
            "user_name": _get_session_user_name(sess),
            "item_id": active_item_id,
            "client": str(sess.get("Client") or "").strip(),
            "device": str(sess.get("DeviceName") or "").strip(),
            "remote_endpoint": str(sess.get("RemoteEndPoint") or "").strip(),
        })

    effective_item_id = str(item_id or "").strip()
    if not effective_item_id and matched_session:
        now_playing = matched_session.get("NowPlayingItem") or {}
        if isinstance(now_playing, dict):
            effective_item_id = str(now_playing.get("Id") or "").strip()

    identity_session = identity_session or {}
    return {
        "user_key": user_key,
        "user_name": user_name,
        "item_id": effective_item_id,
        "session_id": str(identity_session.get("Id") or "").strip(),
        "client": str(identity_session.get("Client") or auth_client or "").strip(),
        "device": str(identity_session.get("DeviceName") or auth_device or "").strip(),
        "remote_endpoint": str(identity_session.get("RemoteEndPoint") or "").strip(),
        "active_user_keys": active_user_keys,
        "active_item_ids": active_item_ids,
        "active_playbacks": active_playbacks,
    }, sessions, matched_session


async def _refresh_playback_topology_later(emby_index: int, emby_cfg: dict, delay: float = 2.0):
    try:
        await asyncio.sleep(delay)
        sessions = await _fetch_emby_sessions(
            str((emby_cfg or {}).get("url") or "").rstrip("/"),
            str((emby_cfg or {}).get("key") or ""),
        )
        drive115_service.update_playback_topology_sessions(emby_index, emby_cfg, sessions)
    except Exception as e:
        logger.debug(f"[Topology] 延迟刷新播放拓扑失败: {type(e).__name__} {repr(e)}")


def _extract_pickcode_from_direct_path(path: str) -> tuple[str, str]:
    normalized = str(path or "").strip().lstrip("/")
    if not normalized.lower().startswith("d/"):
        return "", ""
    tail = normalized[2:]
    if not tail:
        return "", ""
    display_name = tail.split("/", 1)[-1] if "/" in tail else tail
    first_segment = tail.split("/", 1)[0]
    match = re.match(r"^(?P<pickcode>[A-Za-z0-9]+)(?:\.[A-Za-z0-9]{1,8})?$", first_segment)
    if not match:
        return "", display_name
    return match.group("pickcode"), display_name


# ==================================================================
# [辅助] 后台预加载任务
# ==================================================================
async def _preload_rapid_transfer(item_id: str, user_agent: str, item_name: str, emby_index: int = 0, rapid_context: dict | None = None):
    """后台预加载任务 - 直接调用 get_direct_url 获取直链"""
    try:
        cfg = await get_config_302()
        embys = cfg.get("embys", [])
        emby_name = embys[emby_index].get("name", f"Emby[{emby_index}]") if emby_index < len(embys) else f"Emby[{emby_index}]"

        result = await drive115_service.get_direct_url(
            item_id,
            media_source_id=None,
            user_agent=user_agent,
            item_name=item_name,
            emby_index=emby_index,
            rapid_context=rapid_context,
        )
        if result:
            logger.info(f"[预缓存-{emby_name}] 后台预加载成功: {item_name}")
        else:
            logger.debug(f"[预缓存-{emby_name}] 后台预加载未命中: {item_name}")
    except Exception as e:
        logger.warning(f"[预缓存] 后台预加载异常 ({item_name}): {e}")

# ==================================================================
# [辅助] 响应解析任务 (缓存名字)
# ==================================================================
async def handle_response_parsing(data: any, user_agent: str, item_id: str = None):
    """
    解析 Emby API 响应，缓存名字

    注意：预加载已在 PlaybackInfo 触发时直接执行，这里只处理名字缓存
    """
    try:
        if not isinstance(data, dict): return

        # === 场景 A: 列表页 (首页/媒体库) ===
        if "Items" in data and isinstance(data["Items"], list):
            for item in data["Items"]:
                item_id = item.get("Id")
                if item_id:
                    name_cache[item_id] = {"display_name": get_friendly_name(item)}
            return

        # === 场景 B: 单个详情页 ===
        # 优先使用传入的 item_id (PlaybackInfo 响应没有 Id 字段)
        data_item_id = data.get("Id")
        final_item_id = item_id or data_item_id

        item_type = data.get("Type")

        if not final_item_id: return

        # 缓存名字
        friendly_name = name_cache.get(final_item_id)
        if not friendly_name:
            friendly_name = get_friendly_name(data) if item_type else f"ID: {final_item_id}"
            name_cache[final_item_id] = {"display_name": friendly_name}

    except Exception as e:
        logger.debug(f"[Preload] 响应解析异常: {e}")

# ==================================================================
# WebSocket
# ==================================================================
@router.websocket("/embywebsocket")
async def websocket_endpoint(client_ws: WebSocket):
    await client_ws.accept()
    base_url, _, _ = await get_emby_config()
    
    if not base_url:
        logger.warning("[WS] 未配置 Emby URL，关闭连接")
        await client_ws.close()
        return

    ws_base_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    upstream_url = f"{ws_base_url}/embywebsocket"
    
    if client_ws.query_params:
        upstream_url += f"?{client_ws.query_params}"

    try:
        async with websockets.connect(upstream_url) as server_ws:
            async def client_to_server():
                try:
                    while True:
                        data = await client_ws.receive_text()
                        await server_ws.send(data)
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logger.warning(f"[WS] 客户端异常断开: {e}")

            async def server_to_client():
                try:
                    async for message in server_ws:
                        await client_ws.send_text(message)
                except websockets.exceptions.ConnectionClosed:
                    pass
                except Exception as e:
                    if "Unexpected ASGI message" in str(e):
                        pass 
                    else:
                        logger.warning(f"[WS] 服务端异常断开: {e}")

            await asyncio.gather(client_to_server(), server_to_client())

    except Exception as e:
        logger.error(f"[WS] 连接 Emby 失败: {upstream_url} | 原因: {e}")
    finally:
        try:
            await client_ws.close()
        except: 
            pass

# ==================================================================
# HTTP 网关主逻辑
# ==================================================================
@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
async def emby_gateway(request: Request, path: str, background_tasks: BackgroundTasks):
    if path.startswith("api/") or path.startswith("static/") or path.startswith("fonts/"):
        return Response(status_code=404)
    if path == "" or path == "/":
        return RedirectResponse(url="/web/index.html")

    if "api/danmu" in path:
        return Response(status_code=204)

    # 获取请求的本地端口，用于确定使用哪个 Emby 配置
    request_port = None
    if hasattr(request, 'scope') and 'server' in request.scope:
        request_port = request.scope['server'][1]  # (host, port)

    # 根据端口获取对应的 Emby 配置
    emby_cfg, emby_index = await get_emby_config_by_port(request_port)
    base_url = emby_cfg.get("url", "").rstrip("/")
    api_key = emby_cfg.get("key", "")
    emby_name = emby_cfg.get("name", f"Emby[{emby_index}]")

    if not base_url:
        return Response("Emby URL not configured.", status_code=502)

    # -----------------------------
    # 2. 302 播放逻辑
    # -----------------------------
    lower_path = path.lower()
    is_playback = "videos" in lower_path and ("stream" in lower_path or "original" in lower_path)
    pickcode, direct_display_name = _extract_pickcode_from_direct_path(path)
    is_direct_pickcode_playback = bool(pickcode)
    enable_302 = emby_cfg.get("enabled", False)

    if request.method == "GET" and enable_302:
        if is_playback:
            try:
                parts = path.split("/")
                item_id = None
                for i, part in enumerate(parts):
                    if part.lower() == "videos":
                        if i + 1 < len(parts):
                            item_id = parts[i+1]
                            break

                media_source_id = request.query_params.get("mediaSourceId") or request.query_params.get("MediaSourceId")
                user_agent = request.headers.get("user-agent", "")

                # 解析 Emby 授权头（获取客户端、设备和 token 信息）
                emby_auth = request.headers.get("x-emby-authorization", "")
                parsed_auth = _parse_emby_authorization(emby_auth)
                auth_client = parsed_auth.get("client", "")
                auth_device = parsed_auth.get("device", "")
                auth_token = parsed_auth.get("token", "")
                rapid_context, emby_sessions, matched_session = await _build_rapid_context(
                    base_url,
                    api_key,
                    item_id or "",
                    auth_token,
                    auth_client,
                    auth_device,
                    user_agent,
                )
                asyncio.create_task(_refresh_playback_topology_later(emby_index, emby_cfg))

                if item_id:
                    # 尝试从缓存获取媒体信息
                    cached = name_cache.get(item_id)
                    if cached and isinstance(cached, dict) and cached.get("display_name") and cached.get("full_meta"):
                        display_name = cached.get("display_name", "")
                        original_name = cached.get("original_name", "")
                        media_type = cached.get("media_type", "movie")
                        poster_url = cached.get("poster_url", "")
                        overview = cached.get("overview", "")
                        rating = cached.get("rating", "")
                        genres = cached.get("genres", "")
                        tagline = cached.get("tagline", "")
                        media_year = cached.get("year", "")
                    else:
                        display_name = ""
                        original_name = ""
                        media_type = "movie"
                        poster_url = ""
                        overview = ""
                        rating = ""
                        genres = ""
                        tagline = ""
                        media_year = ""

                    # 缓存未命中时调 Emby API 获取媒体信息
                    if not display_name:
                        try:
                            from core.emby_client import EmbyClient
                            emby_client = EmbyClient(
                                host=emby_cfg.get("url", ""),
                                key=emby_cfg.get("key", ""),
                                public_host=emby_cfg.get("public_host")
                            )
                            item_info = emby_client.get_item_info(item_id)
                            if item_info:
                                meta = _format_emby_item_info(item_info, item_id)
                                display_name = meta.get("display_name", "")
                                original_name = meta.get("original_name", "")
                                media_type = meta.get("media_type", "movie")
                                poster_url = meta.get("poster_url", "")
                                overview = meta.get("overview", "")
                                rating = meta.get("rating", "")
                                genres = meta.get("genres", "")
                                tagline = meta.get("tagline", "")
                                media_year = meta.get("year", "")
                                name_cache[item_id] = meta
                        except Exception as e:
                            logger.debug(f"[Gateway] 获取媒体信息失败: {e}")

                    if not display_name:
                        display_name = f"ID: {item_id}"

                    logger.info(f"[网关-{emby_name}] 收到播放请求: {display_name}")
                    direct_url = await drive115_service.get_direct_url(
                        item_id,
                        media_source_id,
                        user_agent,
                        item_name=display_name,
                        emby_index=emby_index,
                        direct_link_context="gateway_playback",
                        rapid_context=rapid_context,
                    )

                    if direct_url:
                        logger.info(f"[网关-{emby_name}] 302重定向到115直链: {display_name}")

                        # 播放通知去重：同一 item_id 30 秒内只通知一次
                        if item_id not in playback_notify_cache:
                            playback_notify_cache[item_id] = True

                            # 获取播放用户名（通过 Emby Sessions API）
                            play_user = ""
                            try:
                                sessions = emby_sessions or []
                                if sessions:
                                    for sess in sessions:
                                        if matched_session is None and str(sess.get("NowPlayingItem", {}).get("Id", "") or "") == str(item_id):
                                            matched_session = sess
                                            break
                                if matched_session is None:
                                    for sess in sessions:
                                        if _session_matches_token(sess, auth_token):
                                            matched_session = sess
                                            logger.debug(f"[Gateway-{emby_name}] 通过会话 token 匹配播放用户: {display_name}")
                                            break
                                if matched_session is None:
                                    for sess in sessions:
                                        if _session_matches_client(sess, auth_client, auth_device, user_agent):
                                            matched_session = sess
                                            logger.debug(f"[Gateway-{emby_name}] 通过客户端信息匹配播放用户: {display_name}")
                                            break
                                if matched_session is not None:
                                    play_user = _get_session_user_name(matched_session)
                                    if not auth_client:
                                        auth_client = str(matched_session.get("Client", "") or "")
                                    if not auth_device:
                                        auth_device = str(matched_session.get("DeviceName", "") or "")
                                else:
                                    logger.debug(f"[Gateway-{emby_name}] 未匹配到播放用户: item={item_id} sessions={len(sessions)} client={auth_client or '-'} device={auth_device or '-'}")
                            except Exception as e:
                                logger.debug(f"[Gateway-{emby_name}] 获取播放用户失败: {type(e).__name__} {repr(e)}")

                            client_info = ""
                            if auth_client:
                                client_info = auth_client
                                if auth_device:
                                    client_info += f" ({auth_device})"
                            if not client_info and user_agent:
                                ua_lower = user_agent.lower()
                                if "infuse" in ua_lower:
                                    client_info = "Infuse"
                                elif "fileball" in ua_lower:
                                    client_info = "Fileball"
                                elif "emby" in ua_lower:
                                    client_info = "Emby 官方客户端"
                                elif "mrp" in ua_lower or "mrplay" in ua_lower:
                                    client_info = "MRP"
                                else:
                                    client_info = user_agent[:30] + "..." if len(user_agent) > 30 else user_agent

                            cached_tmdb_id = ""
                            cached_entry = name_cache.get(item_id)
                            if isinstance(cached_entry, dict):
                                cached_tmdb_id = cached_entry.get("tmdb_id", "")

                            notify_kwargs = dict(
                                item_name=display_name,
                                emby_name=emby_name,
                                user_agent=user_agent,
                                poster_url=poster_url,
                                original_name=original_name,
                                media_type=media_type,
                                tmdb_id=cached_tmdb_id,
                                overview=overview,
                                rating=rating,
                                genres=genres,
                                tagline=tagline,
                                user_name=play_user,
                                client_info=client_info,
                                year=media_year,
                                server_idx=emby_index,
                                item_id=item_id,
                            )

                            def send_playback_notification(kwargs):
                                try:
                                    from app.services.wechat_service import wechat_notify_service
                                    from app.services.telegram_service import telegram_notify_service
                                    wechat_notify_service.notify_playback(**kwargs)
                                    telegram_notify_service.notify_playback(**kwargs)
                                except Exception as notify_err:
                                    logger.debug(f"[Gateway] 发送播放通知失败: {notify_err}")

                            import threading
                            threading.Thread(
                                target=send_playback_notification,
                                args=(notify_kwargs,),
                                daemon=True
                            ).start()
                        else:
                            logger.debug(f"[Gateway-{emby_name}] 播放通知去重: {display_name} (30s内已通知)")

                        return _build_redirect_response(direct_url)
                    else:
                        logger.info(f"[网关-{emby_name}] 115直链获取失败，已降级反向代理: {display_name}")
            except Exception as e:
                logger.error(f"[Gateway] 302 处理异常: {e}")

        elif is_direct_pickcode_playback:
            try:
                user_agent = request.headers.get("user-agent", "")
                emby_auth = request.headers.get("x-emby-authorization", "")
                parsed_auth = _parse_emby_authorization(emby_auth)
                rapid_context, emby_sessions, _ = await _build_rapid_context(
                    base_url,
                    api_key,
                    "",
                    parsed_auth.get("token", ""),
                    parsed_auth.get("client", ""),
                    parsed_auth.get("device", ""),
                    user_agent,
                )
                asyncio.create_task(_refresh_playback_topology_later(emby_index, emby_cfg))
                display_name = _resolve_direct_pickcode_name(pickcode, direct_display_name or f"{pickcode}.mkv")
                redirect_cache_key = f"{pickcode}_{user_agent or 'NoUA'}"
                direct_url = await drive115_service.get_direct_url_by_pickcode(
                    pickcode=pickcode,
                    user_agent=user_agent,
                    emby_index=emby_index,
                    filename=display_name,
                    direct_link_context="gateway_direct",
                    rapid_context=rapid_context,
                )
                if direct_url:
                    if redirect_cache_key not in strm_redirect_cache:
                        strm_redirect_cache[redirect_cache_key] = True
                        logger.info(f"[网关-{emby_name}] 收到 STRM 直连请求: {display_name}")
                        logger.info(f"[网关-{emby_name}] STRM 302重定向到115直链: {display_name}")
                    return _build_redirect_response(direct_url)
                logger.info(f"[网关-{emby_name}] STRM 直链获取失败，已降级反向代理: {display_name}")
            except Exception as e:
                logger.error(f"[Gateway] STRM /d 处理异常: {e}")

    # -----------------------------
    # 3. 反向代理转发
    # -----------------------------
    clean_path = path.lstrip("/")
    target_url = f"{base_url}/{clean_path}"
    
    raw_query = request.scope.get("query_string", b"")
    if raw_query:
        target_url += f"?{raw_query.decode('utf-8')}"

    try:
        remove_headers = {"host", "content-length", "connection", "transfer-encoding", "upgrade"}
        if request.method == "GET":
            remove_headers.add("content-type")

        headers = {
            k: v for k, v in request.headers.items() 
            if k.lower() not in remove_headers
        }

        exclude_keywords = [
            "/Images", "/PlaybackInfo", "/Intros", "/ThemeMedia",
            "/Counts", "/Sessions", "/ScheduledTasks"
        ]

        should_parse_response = (
            request.method == "GET" and
            ("Users/" in path and "/Items" in path) and
            not is_playback and
            not any(sub in path for sub in exclude_keywords)
        )
        
        enable_preload = emby_cfg.get("preload", False)

        # ==================================================================
        # PlaybackInfo 触发逻辑 - 直接后台预加载
        # ==================================================================
        if enable_preload and "PlaybackInfo" in path and "/Items/" in path:
            try:
                parts = path.split("/")
                if "Items" in parts:
                    idx = parts.index("Items")
                    if idx + 1 < len(parts):
                        item_id = parts[idx+1]
                        if item_id and item_id not in preload_dedupe_cache:
                            preload_dedupe_cache[item_id] = True
                            ua = request.headers.get("user-agent", "")
                            emby_auth = request.headers.get("x-emby-authorization", "")
                            parsed_auth = _parse_emby_authorization(emby_auth)
                            rapid_context, sessions, matched_session = await _build_rapid_context(
                                base_url,
                                api_key,
                                item_id,
                                parsed_auth.get("token", ""),
                                parsed_auth.get("client", ""),
                                parsed_auth.get("device", ""),
                                ua,
                            )
                            drive115_service.update_playback_topology_sessions(emby_index, emby_cfg, sessions)
                            p_name = await _resolve_preload_name(item_id, emby_cfg)
                            user_label = rapid_context.get("user_name") or rapid_context.get("user_key") or "未知用户"

                            logger.info(f"[预缓存-{emby_name}] 播放信息接口触发预加载: {p_name} | 用户={user_label} | 正在播放={len(rapid_context.get('active_user_keys') or [])}")

                            # 直接后台预加载，不等待响应
                            asyncio.create_task(
                                _preload_rapid_transfer(item_id, ua, p_name, emby_index, rapid_context=rapid_context)
                            )
            except Exception as e:
                logger.warning(f"[预缓存] 播放信息接口触发失败: {e}")

        # === 模式 A: 需要解析响应（列表页和详情页） ===
        if should_parse_response:
            req = proxy_client.build_request(request.method, target_url, headers=headers, content=None)
            r = await proxy_client.send(req, stream=False)

            try:
                data = r.json()
                ua = request.headers.get("user-agent", "")
                background_tasks.add_task(handle_response_parsing, data, ua)
            except Exception as e:
                if r.status_code != 200:
                    pass
                else:
                    logger.warning(f"[Gateway] 响应解析警告: {e}")

            response_headers = dict(r.headers)
            for key in ["transfer-encoding", "content-length", "connection", "content-encoding"]:
                response_headers.pop(key, None)

            return Response(content=r.content, status_code=r.status_code, headers=response_headers)

        # === 模式 B: 普通/流式请求 ===
        req = None
        if request.method in ["GET", "HEAD", "OPTIONS"]:
            req = proxy_client.build_request(request.method, target_url, headers=headers, content=None)
        elif not is_playback:
            body_content = await request.body()
            req = proxy_client.build_request(request.method, target_url, headers=headers, content=body_content)
        else:
            req = proxy_client.build_request(request.method, target_url, headers=headers, content=request.stream())
        
        r = await proxy_client.send(req, stream=True)
        
        if "Similar" in path and r.status_code >= 500:
            await r.aclose()
            # 静默处理，部分剧集没有相关推荐属于正常情况
            return JSONResponse(content={"Items": [], "TotalRecordCount": 0})

        response_headers = dict(r.headers)
        for key in ["transfer-encoding", "content-length", "connection"]:
            response_headers.pop(key, None)

        return StreamingResponse(
            r.aiter_raw(),
            status_code=r.status_code,
            headers=response_headers
        )
    except asyncio.CancelledError:
        logger.debug(f"[Gateway-{emby_name}] 转发已取消: {request.method} /{clean_path} -> {target_url}")
        raise
    except (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError, httpx.StreamClosed) as e:
        logger.warning(f"[Gateway-{emby_name}] 转发连接中断: {request.method} /{clean_path} -> {target_url} | {_format_proxy_error(e)}")
        return Response(f"Gateway Error: {_format_proxy_error(e)}", status_code=502)
    except ClientDisconnect:
        logger.debug(f"[Gateway-{emby_name}] 客户端已断开转发请求: {request.method} /{clean_path} -> {target_url}")
        return Response(status_code=499)
    except Exception as e:
        logger.error(f"[Gateway-{emby_name}] 转发失败: {request.method} /{clean_path} -> {target_url} | {_format_proxy_error(e)}")
        return Response(f"Gateway Error: {_format_proxy_error(e)}", status_code=502)
