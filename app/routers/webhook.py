# app/routers/webhook.py
import os
import json
import re
from fastapi import APIRouter, Request, HTTPException

from app.schemas import WebhookConfigModel
from app.dependencies import webhook_debouncer
from app.routers.config_302 import get_emby_configs_sync
from app.services.task_service import execute_task_logic
from app.services.webhook_queue import enqueue_webhook_payload, get_webhook_queue_stats
from app.services.wechat_service import wechat_notify_service
from core.configs import WEBHOOK_CONFIG_FILE
from core.emby_client import EmbyClient
from core.logger import logger

router = APIRouter(tags=["Webhook"])


def _default_webhook_config() -> dict:
    return {
        "enabled": False,
        "engine": "classic",
        "preset": "",
        "mode": "random",
        "delete_sync_enabled": True,
    }


def _normalize_webhook_config(data: dict | None = None) -> dict:
    config = {**_default_webhook_config(), **(data if isinstance(data, dict) else {})}
    config["delete_sync_enabled"] = True
    return config


def _is_emby_test_notification(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    values = [
        data.get("Event"),
        data.get("Title"),
        data.get("Description"),
        data.get("Name"),
        data.get("Message"),
    ]
    text = " ".join(str(value or "") for value in values).lower()
    if "test" in text or "测试" in text:
        return True
    if not data.get("Item") and not data.get("Event"):
        return True
    return False


def _extract_tmdb_id_from_webhook_item(item_data: dict) -> str:
    provider_ids = item_data.get("ProviderIds") or {}
    for key in ("Tmdb", "TMDb", "TheMovieDb", "themoviedb"):
        if provider_ids.get(key):
            return str(provider_ids.get(key))
    for item in item_data.get("ExternalUrls") or []:
        url = str(item.get("Url") or "")
        match = re.search(r"themoviedb\.org/(?:tv|movie)/(\d+)", url)
        if match:
            return match.group(1)
    return ""


def _sync_missing_episode_stats_for_series(client: EmbyClient, matched_lib: dict, server_idx: int, series_id: str, series_info: dict | None = None) -> bool:
    if not series_id:
        logger.info("[Webhook] 缺集统计缓存未更新: 缺少 SeriesId")
        return False
    try:
        series_info = series_info or client.get_item_info(series_id)
        if not series_info:
            logger.info(f"[Webhook] 缺集统计缓存未更新: 无法读取剧集信息 Series={series_id}")
            return False
        tmdb_id = str(series_info.get("tmdb_id") or "").strip()
        if not tmdb_id:
            logger.info(
                f"[Webhook] 缺集统计缓存未更新: 剧集缺少 TMDB ID "
                f"Series={series_id} title={series_info.get('name') or ''}"
            )
            return False
        seasons = client.get_series_episode_counts_by_id(series_id)
        from app.services.emby_library_cache import upsert_discover_series_entry
        entry = upsert_discover_series_entry(
            server_idx=server_idx,
            library_id=str(matched_lib.get("id") or ""),
            library_name=str(matched_lib.get("name") or ""),
            emby_id=str(series_id or ""),
            tmdb_id=tmdb_id,
            title=series_info.get("name") or "",
            original_title=series_info.get("original_title") or "",
            year=str(series_info.get("year") or series_info.get("series_year") or ""),
            seasons=seasons,
        )
        if not entry:
            logger.info(f"[Webhook] 缺集统计缓存未更新: 剧集索引写入失败 Series={series_id} TMDB={tmdb_id}")
            return False
        from app.routers.discover import sync_missing_episode_stats_entry
        patched = sync_missing_episode_stats_entry(entry)
        if not patched:
            logger.info(
                f"[Webhook] 缺集统计缓存未更新: 未找到可更新的缺集统计缓存 "
                f"Series={series_id} TMDB={tmdb_id} Library={matched_lib.get('name') or ''}"
            )
        return patched
    except Exception as e:
        logger.warning(f"[Webhook] 缺集统计单剧增量更新失败: Series={series_id} error={e}")
        return False


def _sync_discover_movie_index_for_item(client: EmbyClient, matched_lib: dict, server_idx: int, item_id: str, item_data: dict | None = None) -> bool:
    if not item_id:
        logger.info("[Webhook] 发现页电影索引未更新: 缺少 ItemId")
        return False
    item_data = item_data or {}
    try:
        item_info = client.get_item_info(item_id)
        tmdb_id = str((item_info or {}).get("tmdb_id") or "").strip()
        if not tmdb_id:
            tmdb_id = _extract_tmdb_id_from_webhook_item(item_data)
        if not tmdb_id:
            logger.info(
                f"[Webhook] 发现页电影索引未更新: 电影缺少 TMDB ID "
                f"Item={item_id} title={(item_info or {}).get('name') or item_data.get('Name') or ''}"
            )
            return False
        from app.services.emby_library_cache import upsert_discover_movie_entry
        patched = upsert_discover_movie_entry(
            server_idx=server_idx,
            library_id=str(matched_lib.get("id") or ""),
            library_name=str(matched_lib.get("name") or ""),
            emby_id=str(item_id or ""),
            tmdb_id=tmdb_id,
            title=(item_info or {}).get("name") or item_data.get("Name") or "",
            original_title=(item_info or {}).get("original_title") or item_data.get("OriginalTitle") or "",
            year=str((item_info or {}).get("year") or item_data.get("ProductionYear") or ""),
        )
        if patched:
            logger.info(f"[Webhook] 发现页电影索引已增量写入: Item={item_id} TMDB={tmdb_id}")
        else:
            logger.info(f"[Webhook] 发现页电影索引未更新: 写入失败 Item={item_id} TMDB={tmdb_id}")
        return patched
    except Exception as e:
        logger.warning(f"[Webhook] 发现页电影索引增量更新失败: Item={item_id} error={e}")
        return False


def _sync_missing_episode_stats_for_removed_item(client: EmbyClient, matched_lib: dict | None, server_idx: int, item_data: dict, target_item_id: str) -> bool:
    item_type = item_data.get("Type", "")
    series_id = str(item_data.get("SeriesId") or (target_item_id if item_type == "Series" else "") or "")
    season = item_data.get("ParentIndexNumber")
    episode = item_data.get("IndexNumber")

    if item_type == "Episode" and series_id:
        try:
            series_info = client.get_item_info(series_id)
            if series_info and matched_lib and _sync_missing_episode_stats_for_series(client, matched_lib, server_idx, series_id, series_info=series_info):
                return True
        except Exception:
            pass
        if season is not None and episode is not None:
            from app.services.emby_library_cache import patch_discover_series_episode
            entry = patch_discover_series_episode(emby_id=series_id, season=season, episode=episode, present=False)
            if entry:
                from app.routers.discover import sync_missing_episode_stats_entry
                return sync_missing_episode_stats_entry(entry)

    if item_type == "Series":
        tmdb_id = _extract_tmdb_id_from_webhook_item(item_data)
        if tmdb_id:
            from app.services.emby_library_cache import remove_discover_series_entry
            removed = remove_discover_series_entry(
                tmdb_id=tmdb_id,
                library_id=str((matched_lib or {}).get("id") or ""),
            )
            if removed:
                from app.routers.discover import sync_missing_episode_stats_entry
                return sync_missing_episode_stats_entry(remove=True, tmdb_id=tmdb_id, library_id=str((matched_lib or {}).get("id") or ""))
    if item_type == "Movie":
        tmdb_id = _extract_tmdb_id_from_webhook_item(item_data)
        if tmdb_id:
            from app.services.emby_library_cache import remove_discover_movie_entry
            return remove_discover_movie_entry(
                tmdb_id=tmdb_id,
                library_id=str((matched_lib or {}).get("id") or ""),
            )
    return False

@router.get("/api/webhook/config")
def get_webhook_config():
    if os.path.exists(WEBHOOK_CONFIG_FILE):
        try:
            with open(WEBHOOK_CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return _normalize_webhook_config(data)
        except: pass
    return _default_webhook_config()

@router.post("/api/webhook/config")
def save_webhook_config(cfg: WebhookConfigModel):
    try:
        data = cfg.model_dump()
        data["delete_sync_enabled"] = True
        with open(WEBHOOK_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/webhook")
async def emby_webhook_trigger(request: Request):
    try:
        content_type = request.headers.get("content-type", "")
        data = {}
        if "application/json" in content_type:
            data = await request.json()
        else:
            form = await request.form()
            if 'data' in form:
                data = json.loads(form['data'])
            else:
                data = dict(form)
    except Exception as e:
        return {"status": "error", "reason": f"Payload Error: {e}"}

    event_type = data.get("Event", "")
    item_data = data.get("Item", {}) if isinstance(data.get("Item", {}), dict) else {}
    target_item_id = item_data.get("Id")

    if _is_emby_test_notification(data):
        logger.info(
            "[Webhook] webhook接收到emby测试通知: "
            f"event={event_type or 'unknown'} title={data.get('Title', '')} "
            f"description={str(data.get('Description', ''))[:120]}"
        )
        return {"status": "ok", "action": "emby_test_notification"}

    allowed_events = ["library.new", "item.added", "library.scan_complete"]
    delete_events = ["item.removed", "library.deleted", "deep.delete"]
    if event_type not in allowed_events and event_type not in delete_events:
        return {"status": "ignored", "reason": f"Event '{event_type}' not watched"}

    try:
        job_id = enqueue_webhook_payload(data, event_type=event_type, item_id=str(target_item_id or ""))
        return {"status": "queued", "job_id": job_id}
    except Exception as e:
        logger.error(f"[WebhookQueue] Webhook 入队失败: {e}", exc_info=True)
        return {"status": "error", "reason": f"Queue Error: {e}"}


@router.get("/api/webhook/queue")
def get_webhook_queue():
    return get_webhook_queue_stats()


def process_webhook_payload(data: dict):
    event_type = data.get("Event", "")

    if _is_emby_test_notification(data):
        logger.info(
            "[Webhook] webhook接收到emby测试通知: "
            f"event={event_type or 'unknown'} title={data.get('Title', '')} "
            f"description={str(data.get('Description', ''))[:120]}"
        )
        return {"status": "ok", "action": "emby_test_notification"}

    wh_config = _default_webhook_config()
    if os.path.exists(WEBHOOK_CONFIG_FILE):
        try:
            with open(WEBHOOK_CONFIG_FILE, 'r', encoding='utf-8') as f:
                wh_config = _normalize_webhook_config(json.load(f))
        except:
            return {"status": "error", "reason": "Config Read Error"}

    allowed_events = ["library.new", "item.added", "library.scan_complete"]
    delete_events = ["item.removed", "library.deleted", "deep.delete"]
    delete_sync_events = {"deep.delete"}

    webhook_enabled = bool(wh_config.get("enabled", False))
    delete_sync_enabled = True

    if event_type not in allowed_events and event_type not in delete_events:
        return {"status": "ignored", "reason": f"Event '{event_type}' not watched"}

    item_data = data.get("Item", {})
    target_item_id = item_data.get("Id")
    item_path = item_data.get("Path") 

    if event_type in delete_events:
        logger.info(
            "[Webhook] 收到删除事件: "
            f"event={event_type} type={item_data.get('Type', '')} "
            f"name={item_data.get('Name', '')} path={item_path or ''} "
            f"webhook_enabled={webhook_enabled} delete_sync_enabled={delete_sync_enabled}"
        )
        delete_sync_result = None
        if event_type in delete_sync_events:
            try:
                from app.services.emby_delete_sync import sync_emby_delete_to_115
                delete_sync_result = sync_emby_delete_to_115(data, wh_config)
                if delete_sync_result and delete_sync_result.get("status") != "disabled":
                    logger.info(f"[Webhook] Emby删除同步115结果: {delete_sync_result}")
            except Exception as e:
                logger.error(f"[Webhook] Emby删除同步115失败: {e}", exc_info=True)
        else:
            delete_sync_result = {"status": "skipped", "message": f"非 deep.delete 删除通知不触发115删除: {event_type}"}

        patched = False
        servers = get_emby_configs_sync()
        for svr_idx, svr in enumerate(servers):
            try:
                if not svr.get('enabled', True):
                    continue
                client = EmbyClient(svr['url'], svr['key'], svr.get('public_host'))
                matched_lib = None
                if item_path:
                    norm_item_path = item_path.replace('\\', '/')
                    server_libs = client.get_libraries()
                    matched_lib = next(
                        (lib for lib in server_libs if lib.get('paths') and
                         any(norm_item_path.startswith(loc.replace('\\', '/')) for loc in lib.get('paths'))),
                        None
                    )
                patched = _sync_missing_episode_stats_for_removed_item(client, matched_lib, svr_idx, item_data, target_item_id) or patched
            except Exception as e:
                logger.debug(f"[Webhook] 删除事件缺集统计增量处理失败: {e}")
        return {
            "status": "ok",
            "action": "missing_episode_incremental_delete",
            "patched": patched,
            "delete_sync": delete_sync_result,
        }
    
    if not item_path and target_item_id:
        logger.debug(f"[Webhook] payload缺少路径，准备回查: {target_item_id}")

    event_name_map = {
        "library.new": "媒体入库事件",
        "item.added": "新增条目事件",
        "library.scan_complete": "扫描完成事件"
    }
    event_name = event_name_map.get(event_type, "入库事件")
    logger.info(f"[Webhook] 收到{event_name} (ID: {target_item_id})")

    targets = []
    servers = get_emby_configs_sync()
    stats_patched_count = 0
    discover_index_patched_count = 0

    preset_name = wh_config.get("preset")

    # 遍历服务器，查找该 Webhook 属于哪个库
    for svr_idx, svr in enumerate(servers):
        try:
            if not svr.get('enabled', True):
                continue
            client = EmbyClient(svr['url'], svr['key'], svr.get('public_host'))
            server_libs = client.get_libraries() 
            matched_lib = None
            
            # 1. 尝试直接匹配库ID (如果是 library.new 事件)
            matched_lib = next((l for l in server_libs if str(l['id']) == str(target_item_id)), None)

            # 2. 尝试匹配路径 (如果是 item.added 事件)
            if not matched_lib and item_path:
                norm_item_path = item_path.replace('\\', '/')
                matched_lib = next(
                    (lib for lib in server_libs if lib.get('paths') and 
                     any(norm_item_path.startswith(loc.replace('\\', '/')) for loc in lib.get('paths'))),
                    None
                )

            # 3. 尝试反查 API 获取路径再匹配
            if not matched_lib and target_item_id and not item_path:
                try:
                    full_info = client._request("GET", f"emby/Items/{target_item_id}")
                    fetched_path = full_info.get("Path")
                    if fetched_path:
                        norm_fetched = fetched_path.replace('\\', '/')
                        matched_lib = next(
                            (lib for lib in server_libs if lib.get('paths') and 
                             any(norm_fetched.startswith(loc.replace('\\', '/')) for loc in lib.get('paths'))),
                            None
                        )
                except: pass

            if matched_lib:
                item_type_for_stats = item_data.get("Type", "")
                try:
                    series_id_for_stats = ""
                    if item_type_for_stats == "Episode":
                        series_id_for_stats = str(item_data.get("SeriesId") or "")
                    elif item_type_for_stats == "Series":
                        series_id_for_stats = str(target_item_id or "")
                    if series_id_for_stats:
                        patched = _sync_missing_episode_stats_for_series(client, matched_lib, svr_idx, series_id_for_stats)
                        if patched:
                            stats_patched_count += 1
                            logger.info(f"[Webhook] 缺集统计缓存已增量写入: Series={series_id_for_stats}")
                    elif item_type_for_stats in {"Episode", "Series"}:
                        logger.info(
                            f"[Webhook] 缺集统计缓存未更新: 入库事件缺少 SeriesId "
                            f"type={item_type_for_stats} item={target_item_id}"
                        )
                    elif item_type_for_stats == "Movie":
                        if _sync_discover_movie_index_for_item(client, matched_lib, svr_idx, str(target_item_id or ""), item_data):
                            discover_index_patched_count += 1
                except Exception as index_err:
                    logger.warning(f"[Webhook] 入库索引增量更新失败: {index_err}")

                # 发送入库通知
                try:
                    item_name = item_data.get("Name", "未知媒体")
                    item_type = item_data.get("Type", "")
                    year = item_data.get("ProductionYear", "")
                    media_type = "movie" if item_type == "Movie" else "series" if item_type == "Episode" or item_type == "Series" else "other"
                    poster_url = ""
                    season = ""
                    episode = ""
                    original_name = ""# 用于 TMDB 搜索
                    overview = ""
                    rating = ""
                    genres = ""
                    tagline = ""

                    # 从 payload 直接提取额外字段
                    import re as _re
                    server_name = data.get("Server", {}).get("Name", "")
                    original_title = item_data.get("OriginalTitle", "")
                    external_urls = item_data.get("ExternalUrls", [])
                    tmdb_url = next((u.get("Url", "") for u in external_urls if u.get("Name") == "TheMovieDb"), "")
                    premiere_raw = item_data.get("PremiereDate", "")
                    premiere_date = premiere_raw[:10] if premiere_raw else ""
                    _status_raw = item_data.get("Status", "")
                    status = {"Continuing": "连载中", "Ended": "已完结"}.get(_status_raw, _status_raw)
                    _count_match = _re.search(r'(\d+)\s*项', data.get("Title", ""))
                    item_count = _count_match.group(1) if _count_match else ""

                    # 获取媒体详情和海报
                    if target_item_id and media_type in ["movie", "series"]:
                        try:
                            import re as _re2

                            if item_type == "Episode" and item_data.get("SeriesName"):
                                # ── 单集 Episode ──────────────────────────────────────
                                # payload 已有 SeriesName/季号/集号，只调一次 Series API
                                series_name   = item_data.get("SeriesName", "")
                                season        = str(item_data.get("ParentIndexNumber", "?"))
                                episode       = str(item_data.get("IndexNumber", "?"))
                                original_name = item_data.get("OriginalTitle") or series_name
                                item_name     = f"{series_name} S{season}E{episode}"

                                series_id_pl = item_data.get("SeriesId", "")
                                if series_id_pl:
                                    series_info = client.get_item_info(series_id_pl)
                                    if series_info:
                                        overview   = series_info.get("overview", "") or ""
                                        tagline    = series_info.get("tagline", "") or ""
                                        cr         = series_info.get("community_rating")
                                        rating     = str(round(cr, 1)) if cr else ""
                                        genres     = series_info.get("genres", "") or ""
                                        year       = series_info.get("year", year) or year
                                        if not original_name or original_name == series_name:
                                            original_name = series_info.get("original_title") or series_name
                                        poster_url = series_info.get("poster_url") or ""
                                        if not tmdb_url:
                                            tid = series_info.get("tmdb_id", "")
                                            if tid:
                                                tmdb_url = f"https://www.themoviedb.org/tv/{tid}"
                                        if not status:
                                            _s = series_info.get("status", "")
                                            status = {"Continuing": "连载中", "Ended": "已完结"}.get(_s, _s)
                                elif item_data.get("SeriesPrimaryImageTag"):
                                    poster_url = (
                                        f"{client.public_host}/emby/Items/{series_id_pl}"
                                        f"/Images/Primary?tag={item_data['SeriesPrimaryImageTag']}"
                                        f"&quality=90&maxHeight=500"
                                    )

                            elif item_type == "Series":
                                # ── 分组模式 library.new (Type=Series) ───────────────
                                # payload 已含全量元数据，0 次 API 调用
                                original_name = item_data.get("OriginalTitle") or item_data.get("Name", item_name)
                                item_name     = item_data.get("Name", item_name)
                                overview      = item_data.get("Overview", "") or ""
                                tagline       = (item_data.get("Taglines") or [""])[0]
                                cr            = item_data.get("CommunityRating")
                                rating        = str(round(cr, 1)) if cr else ""
                                genres        = ", ".join(item_data.get("Genres", [])) if item_data.get("Genres") else ""
                                year          = item_data.get("ProductionYear", year) or year

                                # 海报
                                img_tags = item_data.get("ImageTags", {})
                                if "Primary" in img_tags:
                                    poster_url = (
                                        f"{client.public_host}/emby/Items/{target_item_id}"
                                        f"/Images/Primary?tag={img_tags['Primary']}&quality=90&maxHeight=500"
                                    )

                                # 集数区间：直接解析 Description（Emby 已算好）
                                desc_str  = data.get("Description", "")
                                ep_range  = ""
                                if desc_str:
                                    first_line = desc_str.split("\n")[0].strip()
                                    if _re2.match(r'S\d+', first_line):
                                        ep_range = first_line
                                if ep_range:
                                    item_name = f"{item_name} {ep_range}"
                                    logger.info(f"[Webhook] library.new 分组通知: {item_name}")
                                else:
                                    # Fallback：查最近入库集数
                                    try:
                                        from app.dependencies import format_episode_range
                                        from collections import defaultdict
                                        recent_eps = client.get_recently_added_episodes(target_item_id)
                                        if recent_eps:
                                            seasons_map = defaultdict(list)
                                            for ep in recent_eps:
                                                seasons_map[ep["season"]].append(ep["episode"])
                                            parts = []
                                            for s in sorted(seasons_map):
                                                r = format_episode_range(seasons_map[s])
                                                parts.append(f"S{str(s).zfill(2)} {r}")
                                            item_name = f"{item_name} {' / '.join(parts)}"
                                            logger.info(f"[Webhook] Series 分组通知(fallback): {item_name}")
                                    except Exception as ep_err:
                                        logger.debug(f"[Webhook] 查询最近集数失败: {ep_err}")

                            else:
                                # ── 电影 Movie ────────────────────────────────────────
                                item_info = client.get_item_info(target_item_id)
                                if item_info:
                                    year          = item_info.get("year", year)
                                    poster_url    = item_info.get("poster_url") or ""
                                    overview      = item_info.get("overview", "") or ""
                                    tagline       = item_info.get("tagline", "") or ""
                                    cr            = item_info.get("community_rating")
                                    rating        = str(round(cr, 1)) if cr else ""
                                    genres        = item_info.get("genres", "") or ""
                                    item_name     = item_info.get("name", item_name)
                                    original_name = item_info.get("original_title") or item_name

                        except Exception as e:
                            logger.debug(f"[Webhook] 获取媒体详情失败: {e}")

                    if media_type in ["movie", "series"]:
                        from app.services.wechat_service import wechat_notify_service
                        from app.services.telegram_service import telegram_notify_service

                        # 剧集且集数可解析 → 走聚合器，合并多集后发一条通知
                        if (media_type == "series"
                                and season not in ("", "?")
                                and episode not in ("", "?")):
                            from app.dependencies import episode_notify_aggregator
                            agg_key = f"{original_name}_S{season}_{matched_lib['name']}"
                            agg_meta = dict(
                                series_name=series_name,
                                season=season,
                                library_name=matched_lib['name'],
                                year=str(year) if year else "",
                                poster_url=poster_url,
                                original_name=original_name,
                                overview=overview,
                                rating=rating,
                                genres=genres,
                                tagline=tagline,
                                server_name=server_name,
                                original_title=original_title,
                                tmdb_url=tmdb_url,
                                premiere_date=premiere_date,
                                status=status,
                                item_count=item_count,
                                server_idx=svr_idx,
                                item_id=str(target_item_id) if target_item_id else "",
                            )
                            episode_notify_aggregator.add(agg_key, episode, agg_meta)
                            logger.info(f"[Webhook] 聚合待发送: {original_name} S{season}E{episode}")
                        else:
                            # 电影 / 无集数信息的剧集 → 直接发
                            notify_kwargs = dict(
                                media_name=item_name,
                                media_type=media_type,
                                library_name=matched_lib['name'],
                                year=str(year) if year else "",
                                poster_url=poster_url,
                                original_name=original_name,
                                overview=overview,
                                rating=rating,
                                genres=genres,
                                tagline=tagline,
                                server_name=server_name,
                                original_title=original_title,
                                tmdb_url=tmdb_url,
                                premiere_date=premiere_date,
                                status=status,
                                item_count=item_count,
                                server_idx=svr_idx,
                                item_id=str(target_item_id) if target_item_id else "",
                            )
                            wechat_notify_service.notify_media_added(**notify_kwargs)
                            telegram_notify_service.notify_media_added(**notify_kwargs)
                except Exception as notify_err:
                    logger.debug(f"[Webhook] 发送入库通知失败: {notify_err}")

                targets.append({
                    "url": svr['url'],
                    "key": svr['key'],
                    "public_host": svr.get('public_host'),
                    "library_id": matched_lib['id'],
                    "library_name": matched_lib['name'],
                    "server_idx": svr_idx,
                })
            else:
                pass

        except Exception as e:
            logger.error(f"-> Error checking server {svr.get('name')}: {e}")

    if not targets:
        return {"status": "ignored", "reason": f"Item not resolved to any library"}

    if not preset_name:
        return {
            "status": "ok",
            "action": "missing_episode_incremental_update",
            "targets_matched": len(targets),
            "stats_patched": stats_patched_count,
            "discover_index_patched": discover_index_patched_count,
            "reason": "No Preset Selected",
        }

    if not webhook_enabled:
        return {
            "status": "ok",
            "action": "cover_replace_disabled",
            "targets_matched": len(targets),
            "stats_patched": stats_patched_count,
            "discover_index_patched": discover_index_patched_count,
        }

    mode = wh_config.get("mode", "random")
    triggered_count = 0
    
    # 使用防抖器调度任务
    for target in targets:
        lib_id = target['library_id']
        lib_name = target['library_name']

        webhook_debouncer.schedule(
            lib_id,
            execute_task_logic,
            [preset_name, [target], mode, f"Webhook: {lib_name}"],
            display_name=lib_name
        )
        triggered_count += 1

    return {
        "status": "queued",
        "targets_debounced": triggered_count,
        "stats_patched": stats_patched_count,
        "discover_index_patched": discover_index_patched_count,
    }
