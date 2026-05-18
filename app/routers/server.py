# app/routers/server.py
import os
import json
import time
import threading
import psutil
from datetime import datetime
from p115client import P115Client
from p115client.const import SSOENT_TO_APP
from fastapi import APIRouter, HTTPException, Body
from app.schemas import ConnectionRequest, EmbySearchRequest, EmbyItemImagesRequest, EmbyRandomPoolRequest
from core.configs import CONFIG_FILE, TASKS_FILE, RSS_TASKS_FILE, BACKUPS_DIR, FONTS_DIR
from core.emby_client import EmbyClient
from app.dependencies import apply_proxy_settings

router = APIRouter(tags=["Server"])

_DEVICE_METRICS_LOCK = threading.Lock()
_DEVICE_METRICS_SAMPLE = {
    "timestamp": None,
    "net": None,
    "disk": None,
}
_DASHBOARD_OVERVIEW_CACHE_LOCK = threading.Lock()
_DASHBOARD_OVERVIEW_CACHE_TTL = 300
_DASHBOARD_OVERVIEW_CACHE = {}
DASHBOARD_RECENT_ITEM_LIMIT = 48
DASHBOARD_RECENT_PLAYBACK_LIMIT = 30

psutil.cpu_percent(interval=None)


def _bytes_to_human_rate(value):
    value = max(0.0, float(value or 0.0))
    units = ["B/s", "KB/s", "MB/s", "GB/s", "TB/s"]
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    precision = 0 if unit_index == 0 else 1
    return f"{value:.{precision}f} {units[unit_index]}"


def _bytes_to_gb(value):
    return round(float(value or 0) / (1024 ** 3), 1)


def _bytes_to_human_size(value):
    value = max(0.0, float(value or 0.0))
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    precision = 0 if unit_index == 0 else 2 if unit_index >= 4 else 1
    return f"{value:.{precision}f}{units[unit_index]}"


def _safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_str(value, default=""):
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _format_unix_ms_timestamp(value):
    timestamp = _safe_int(value)
    if timestamp <= 0:
        return None
    try:
        if timestamp > 10 ** 12:
            dt = datetime.fromtimestamp(timestamp / 1000)
        elif timestamp > 10 ** 10:
            dt = datetime.fromtimestamp(timestamp)
        else:
            return None
        return dt.isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _build_dashboard_115_payload(message="", **overrides):
    payload = {
        "connected": False,
        "account_name": "115 网盘",
        "uid": "--",
        "login_app": "",
        "login_app_label": "",
        "vip_active": False,
        "vip_label": "未连接",
        "vip_forever": False,
        "vip_expire_at": None,
        "used_bytes": 0,
        "total_bytes": 0,
        "remain_bytes": 0,
        "used_human": "--",
        "total_human": "--",
        "remain_human": "--",
        "usage_percent": 0.0,
        "message": message,
        "timestamp": int(time.time() * 1000),
    }
    payload.update(overrides)
    return payload


def _load_primary_115_drive_config():
    config_302_path = os.path.join(os.getcwd(), "config", "config_302.json")
    if not os.path.exists(config_302_path):
        return {}
    try:
        with open(config_302_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    drives = data.get("drives") if isinstance(data, dict) else None
    if isinstance(drives, list) and drives:
        return drives[0] or {}
    return {}


def _format_115_login_app_label(app: str) -> str:
    app = (app or "").strip()
    if not app:
        return ""
    mapping = {
        "web": "115生活(网页版)",
        "desktop": "115浏览器",
        "android": "115生活(Android端)",
        "ios": "115生活(iOS端)",
        "ipad": "115生活(iPad端)",
        "115android": "115网盘(Android端)",
        "115ios": "115网盘(iOS端)",
        "115ipad": "115网盘(iPad端)",
        "tv": "115生活(Android电视端)",
        "apple_tv": "115生活(Apple TV端)",
        "qandroid": "115管理(Android端)",
        "qios": "115管理(iOS端)",
        "qipad": "115管理(iPad端)",
        "windows": "115生活(Windows端)",
        "os_windows": "115生活(Windows端)",
        "mac": "115生活(macOS端)",
        "os_mac": "115生活(macOS端)",
        "linux": "115生活(Linux端)",
        "os_linux": "115生活(Linux端)",
        "wechatmini": "115生活(微信小程序)",
        "alipaymini": "115生活(支付宝小程序)",
        "harmony": "115网盘(鸿蒙端)",
    }
    return mapping.get(app, app)


def _get_dashboard_overview_cache_key(req: ConnectionRequest) -> str:
    return "|".join([
        _safe_str(req.url),
        _safe_str(req.public_host),
        _safe_str(req.key)[-8:],
    ])



def _get_dashboard_overview_cached(req: ConnectionRequest):
    cache_key = _get_dashboard_overview_cache_key(req)
    if not cache_key:
        return None
    now = time.time()
    with _DASHBOARD_OVERVIEW_CACHE_LOCK:
        payload = _DASHBOARD_OVERVIEW_CACHE.get(cache_key)
        if not payload:
            return None
        if now - payload.get("updated_at", 0) >= _DASHBOARD_OVERVIEW_CACHE_TTL:
            _DASHBOARD_OVERVIEW_CACHE.pop(cache_key, None)
            return None
        return payload.get("data")



def _set_dashboard_overview_cached(req: ConnectionRequest, data: dict):
    cache_key = _get_dashboard_overview_cache_key(req)
    if not cache_key:
        return
    with _DASHBOARD_OVERVIEW_CACHE_LOCK:
        _DASHBOARD_OVERVIEW_CACHE[cache_key] = {
            "updated_at": time.time(),
            "data": data,
        }


def _truncate_dashboard_text(value, max_length: int) -> str:
    text = _safe_str(value)
    if len(text) <= max_length:
        return text
    return text[:max(0, max_length - 3)].rstrip() + "..."


def _compact_dashboard_recent_items(items: list) -> list:
    compacted = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        next_item = dict(item)
        next_item.pop("episode_groups", None)
        next_item["overview"] = _truncate_dashboard_text(next_item.get("overview", ""), 240)
        if next_item.get("media_type") == "tv":
            next_item["episode_label"] = _truncate_dashboard_text(next_item.get("episode_label") or "剧集", 120)
        compacted.append(next_item)
    return compacted


def _extract_115_storage(login_data, space_data, storage_data):
    login_space = ((login_data or {}).get("sapce") or {}).get("1") or {}
    space_payload = space_data or {}
    storage_payload = storage_data or {}

    total_bytes = _safe_int((space_payload.get("all_total") or {}).get("size"), default=0)
    used_bytes = _safe_int((space_payload.get("all_use") or {}).get("size"), default=0)
    remain_bytes = _safe_int((space_payload.get("all_remain") or {}).get("size"), default=0)

    if total_bytes <= 0:
        total_bytes = _safe_int(login_space.get("byte_size_total"), default=_safe_int((storage_payload.get("1") or {}).get("total")))
    if used_bytes <= 0:
        used_bytes = _safe_int(login_space.get("byte_size_used"), default=_safe_int((storage_payload.get("1") or {}).get("used")))
    if remain_bytes <= 0 and total_bytes > 0:
        remain_bytes = _safe_int(login_space.get("byte_size_remain"), default=max(total_bytes - used_bytes, 0))

    if remain_bytes <= 0 and total_bytes > 0:
        remain_bytes = max(total_bytes - used_bytes, 0)

    usage_percent = round((used_bytes / total_bytes) * 100, 1) if total_bytes > 0 else 0.0
    return {
        "used_bytes": used_bytes,
        "total_bytes": total_bytes,
        "remain_bytes": remain_bytes,
        "used_human": _bytes_to_human_size(used_bytes) if total_bytes > 0 else "--",
        "total_human": _bytes_to_human_size(total_bytes) if total_bytes > 0 else "--",
        "remain_human": _bytes_to_human_size(remain_bytes) if total_bytes > 0 else "--",
        "usage_percent": usage_percent,
    }


def _counter_delta_per_second(current, previous, attr, elapsed):
    if not current or not previous or elapsed <= 0:
        return 0.0
    delta = float(getattr(current, attr, 0) - getattr(previous, attr, 0))
    if delta < 0:
        return 0.0
    return delta / elapsed

@router.post("/api/connect")
def connect(req: ConnectionRequest):
    client = EmbyClient(req.url, req.key, req.public_host)
    if client.test_connection(require_library_access=True):
        return {"status": "ok", "libraries": client.get_libraries(), "server_id": client.get_server_id()}
    raise HTTPException(status_code=400, detail="Emby 连接失败，或当前 API Key 无权访问媒体库")

@router.post("/api/library_covers")
def get_library_covers(req: ConnectionRequest):
    client = EmbyClient(req.url, req.key, req.public_host)
    libs = client.get_libraries_with_covers() if hasattr(client, 'get_libraries_with_covers') else client.get_libraries()
    return {"libraries": libs, "server_id": client.get_server_id()}

@router.post("/api/emby/search")
def emby_search(req: EmbySearchRequest):
    client = EmbyClient(req.url, req.key, req.public_host)
    return {"items": client.search_items(req.query, req.library_id, req.type)}

@router.post("/api/emby/get_images")
def emby_get_images(req: EmbyItemImagesRequest):
    client = EmbyClient(req.url, req.key, req.public_host)
    return {"images": client.get_item_images(req.item_id, req.type)}

@router.post("/api/emby/random_pool")
def emby_random_pool(req: EmbyRandomPoolRequest):
    client = EmbyClient(req.url, req.key, req.public_host)
    return {"images": client.get_random_pool(req.library_id, req.type, req.limit)}

@router.post("/api/save")
def save_config(config: dict):
    current_config = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f: current_config = json.load(f)
        except: pass

    # 兼容旧字段 debug_mode，统一映射为 log_level
    if "debug_mode" in config and "log_level" not in config:
        config["log_level"] = "DEBUG" if bool(config.get("debug_mode")) else "INFO"

    if "log_level" in config:
        level = str(config.get("log_level", "INFO")).upper()
        config["log_level"] = level if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO"

    old_proxy_url = current_config.get("proxy_url")
    old_tmdb_key = current_config.get("tmdb_key")

    current_config.update(config)

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(current_config, f, indent=4, ensure_ascii=False)

    # 刷新全局配置缓存
    from core.configs import global_config
    global_config.load()

    # 保存后立即应用日志级别
    if "log_level" in config or "debug_mode" in config:
        from core.logger import set_log_level
        set_log_level(current_config.get("log_level", "INFO"))

    # 重置 douban 单例（代理可能已变）
    from app.routers import discover
    discover._douban_api_instance = None

    # 仅在代理相关字段实际变化时才刷新代理设置
    proxy_changed = (
        ("proxy_url" in config and current_config.get("proxy_url") != old_proxy_url) or
        ("tmdb_key" in config and current_config.get("tmdb_key") != old_tmdb_key)
    )
    if proxy_changed:
        apply_proxy_settings()
    return {"status": "saved", "log_level": current_config.get("log_level", "INFO")}

@router.get("/api/load")
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f: return json.load(f)
    return {}

@router.get("/api/dashboard_stats")
def get_dashboard_stats():
    task_count = 0
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, 'r') as f:
                task_count = len(json.load(f))
        except: pass
    rss_count = 0
    if os.path.exists(RSS_TASKS_FILE):
        try:
            with open(RSS_TASKS_FILE, 'r') as f:
                rss_count = len(json.load(f))
        except: pass
    suite_count = len([d for d in os.listdir(BACKUPS_DIR) if os.path.isdir(os.path.join(BACKUPS_DIR, d))]) if os.path.exists(BACKUPS_DIR) else 0
    font_count = len([f for f in os.listdir(FONTS_DIR) if f.lower().endswith(('.ttf', '.otf'))]) if os.path.exists(FONTS_DIR) else 0
    return {"tasks": task_count + rss_count, "backups": suite_count, "fonts": font_count}

@router.post("/api/dashboard_emby_overview")
def get_dashboard_emby_overview(req: ConnectionRequest):
    cached = _get_dashboard_overview_cached(req)
    if cached is not None:
        return cached

    client = EmbyClient(req.url, req.key, req.public_host)
    try:
        data = {
            "recent_items": _compact_dashboard_recent_items(client.get_recently_added_items(limit=DASHBOARD_RECENT_ITEM_LIMIT)),
            "recent_playbacks": client.get_recent_playbacks(limit=DASHBOARD_RECENT_PLAYBACK_LIMIT),
            "media_stats": client.get_dashboard_media_stats()
        }
        _set_dashboard_overview_cached(req, data)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/dashboard_device_metrics")
def get_dashboard_device_metrics():
    now = time.time()
    with _DEVICE_METRICS_LOCK:
        previous_timestamp = _DEVICE_METRICS_SAMPLE["timestamp"]
        previous_net = _DEVICE_METRICS_SAMPLE["net"]
        previous_disk = _DEVICE_METRICS_SAMPLE["disk"]

        current_net = psutil.net_io_counters()
        current_disk = psutil.disk_io_counters()
        _DEVICE_METRICS_SAMPLE["timestamp"] = now
        _DEVICE_METRICS_SAMPLE["net"] = current_net
        _DEVICE_METRICS_SAMPLE["disk"] = current_disk

    elapsed = now - previous_timestamp if previous_timestamp else 0.0
    if elapsed < 0.5:
        elapsed = 0.0

    memory = psutil.virtual_memory()
    cpu_percent = psutil.cpu_percent(interval=None)

    net_up = _counter_delta_per_second(current_net, previous_net, "bytes_sent", elapsed)
    net_down = _counter_delta_per_second(current_net, previous_net, "bytes_recv", elapsed)
    disk_read = _counter_delta_per_second(current_disk, previous_disk, "read_bytes", elapsed)
    disk_write = _counter_delta_per_second(current_disk, previous_disk, "write_bytes", elapsed)

    return {
        "cpu": {
            "percent": round(float(cpu_percent or 0.0), 1),
        },
        "memory": {
            "percent": round(float(getattr(memory, "percent", 0.0) or 0.0), 1),
            "used_gb": _bytes_to_gb(getattr(memory, "used", 0)),
            "total_gb": _bytes_to_gb(getattr(memory, "total", 0)),
        },
        "network": {
            "up_bytes_per_sec": round(net_up, 1),
            "down_bytes_per_sec": round(net_down, 1),
            "up_human": _bytes_to_human_rate(net_up),
            "down_human": _bytes_to_human_rate(net_down),
        },
        "disk": {
            "read_bytes_per_sec": round(disk_read, 1),
            "write_bytes_per_sec": round(disk_write, 1),
            "read_human": _bytes_to_human_rate(disk_read),
            "write_human": _bytes_to_human_rate(disk_write),
        },
        "timestamp": int(now * 1000),
    }

@router.get("/api/dashboard_115_account")
def get_dashboard_115_account():
    drive_cfg = _load_primary_115_drive_config()
    configured_name = _safe_str(drive_cfg.get("name"), "115 网盘")
    cookie = _safe_str(drive_cfg.get("cookie"))
    if not cookie:
        return _build_dashboard_115_payload(message="未配置 115 账号", account_name=configured_name)

    try:
        client = P115Client(cookie)
        login_info = client.login_info() or {}
        login_data = login_info.get("data") or {}
        user_info = client.user_info() or {}
        user_data = user_info.get("data") or {}
        user_my = client.user_my() or {}
        user_my_data = user_my.get("data") or {}
        space_info = client.user_space_info() or {}
        space_data = space_info.get("data") or {}
        storage_data = client.fs_storage_info() or {}

        account_name = _safe_str(
            user_data.get("user_name")
            or user_data.get("user_name_prepub")
            or user_my_data.get("user_name")
            or login_data.get("user_name"),
            configured_name,
        )
        uid = _safe_str(
            user_data.get("display_uid")
            or user_data.get("user_id")
            or user_my_data.get("display_uid")
            or user_my_data.get("user_id")
            or login_data.get("user_id"),
            "--",
        )
        login_app = _safe_str(client.login_app() or SSOENT_TO_APP.get(client.login_ssoent) or "")
        login_app_label = _format_115_login_app_label(login_app)

        vip_forever = bool(
            user_my_data.get("forever")
            or login_data.get("is_forever")
        )
        vip_active = bool(
            vip_forever
            or _safe_int(user_my_data.get("vip")) > 0
            or _safe_int(user_data.get("is_vip")) > 0
            or _safe_int(login_data.get("is_vip")) > 0
        )
        vip_expire_at = _format_unix_ms_timestamp(
            user_my_data.get("expire")
            or login_data.get("expire")
        )
        vip_label = "永久 VIP" if vip_forever else "VIP" if vip_active else "普通用户"

        storage = _extract_115_storage(login_data, space_data, storage_data)
        return _build_dashboard_115_payload(
            connected=True,
            account_name=account_name,
            uid=uid,
            login_app=login_app,
            login_app_label=login_app_label,
            vip_active=vip_active,
            vip_label=vip_label,
            vip_forever=vip_forever,
            vip_expire_at=vip_expire_at,
            message="",
            **storage,
        )
    except Exception as e:
        detail = str(e)
        message = "Cookie 无效或已过期" if detail else "115 账号信息获取失败"
        return _build_dashboard_115_payload(message=message, account_name=configured_name)

@router.post("/api/server/restart")
async def restart_server():
    """重启服务（用于网关端口变更后生效）"""
    import sys
    import asyncio
    from core.logger import logger

    logger.info("[System] 收到重启请求，将在 1 秒后重启...")

    async def _restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    asyncio.create_task(_restart())
    return {"status": "restarting"}
