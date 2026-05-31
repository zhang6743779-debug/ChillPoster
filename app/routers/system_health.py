import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
import psutil
from fastapi import APIRouter

from app.routers.config_302 import get_config_302_sync
from core.configs import BACKUPS_DIR, CONFIG_FILE, FONTS_DIR, LAYOUTS_DIR, RSS_TASKS_FILE, TEMPLATES_DIR, TASKS_FILE
from core.configs import global_config
from core.emby_client import EmbyClient

router = APIRouter(prefix="/api/system_health", tags=["SystemHealth"])

NETWORK_CHECK_TIMEOUT = 5.0
NETWORK_CHECK_WORKERS = 10
NETWORK_USER_AGENT = "ChillPoster Network Check"

STATIC_NETWORK_TARGETS = [
    ("tmdb_api", "TMDB API", "https://api.themoviedb.org", "元数据", "fa-solid fa-film", True),
    ("tmdb_image", "TMDB 图片 CDN", "https://image.tmdb.org", "元数据", "fa-solid fa-image", True),
    ("tmdb_web", "TMDB 网页", "https://www.themoviedb.org", "元数据", "fa-solid fa-clapperboard", True),
    ("fanart", "Fanart 图片源", "https://webservice.fanart.tv", "元数据", "fa-solid fa-panorama", True),
    ("douban_frodo", "豆瓣 Frodo", "https://frodo.douban.com", "发现源", "fa-solid fa-d", True),
    ("douban_api", "豆瓣 API", "https://api.douban.com", "发现源", "fa-solid fa-database", True),
    ("douban_web", "豆瓣网页", "https://movie.douban.com", "发现源", "fa-solid fa-ticket", True),
    ("bangumi_api", "Bangumi API", "https://api.bgm.tv", "发现源", "fa-solid fa-tv", True),
    ("bangumi_web", "Bangumi 网页", "https://bgm.tv", "发现源", "fa-solid fa-tv", True),
    ("bilibili_api", "哔哩哔哩 API", "https://api.bilibili.com", "发现源", "fa-solid fa-play", True),
    ("bilibili_image", "哔哩哔哩图片", "https://i0.hdslb.com", "发现源", "fa-solid fa-image", True),
    ("mgtv_api", "芒果 TV API", "https://pianku.api.mgtv.com", "发现源", "fa-solid fa-play", True),
    ("tencent_video_api", "腾讯视频 API", "https://pbaccess.video.qq.com", "发现源", "fa-solid fa-play", True),
    ("tencent_video_web", "腾讯视频网页", "https://v.qq.com", "发现源", "fa-solid fa-video", True),
    ("telegram_bot", "Telegram Bot API", "https://api.telegram.org", "通知", "fa-brands fa-telegram", True),
    ("telegram_account", "Telegram 账号授权", "https://my.telegram.org", "通知", "fa-brands fa-telegram", True),
    ("wechat_work", "企业微信 API", "https://qyapi.weixin.qq.com", "通知", "fa-brands fa-weixin", False),
    ("drive115_proapi", "115 Pro API", "https://proapi.115.com", "115", "fa-solid fa-cloud", False),
    ("drive115_webapi", "115 Web API", "https://webapi.115.com", "115", "fa-solid fa-cloud", False),
    ("drive115_web", "115 网页", "https://115.com", "115", "fa-solid fa-cloud", False),
    ("docker_hub", "Docker Hub", "https://hub.docker.com", "升级", "fa-brands fa-docker", True),
    ("docker_registry", "Docker Registry", "https://registry-1.docker.io/v2/", "升级", "fa-brands fa-docker", True),
    ("docker_auth", "Docker Auth", "https://auth.docker.io", "升级", "fa-brands fa-docker", True),
    ("github_web", "GitHub", "https://github.com", "升级", "fa-brands fa-github", True),
    ("github_api", "GitHub API", "https://api.github.com", "升级", "fa-brands fa-github", True),
    ("github_codeload", "GitHub Codeload", "https://codeload.github.com", "升级", "fa-brands fa-github", True),
    ("github_raw", "GitHub Raw", "https://raw.githubusercontent.com", "升级", "fa-brands fa-github", True),
    ("pypi", "PyPI", "https://pypi.org", "依赖", "fa-brands fa-python", True),
    ("pythonhosted", "Python 包文件", "https://files.pythonhosted.org", "依赖", "fa-brands fa-python", True),
    ("cloudflare_cdn", "Font Awesome CDN", "https://cdnjs.cloudflare.com", "前端资源", "fa-solid fa-icons", True),
]


def _read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _json_file_state(path: str) -> tuple[bool, str]:
    if not os.path.exists(path):
        return True, "未创建"
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        return True, "正常"
    except Exception as e:
        return False, str(e) or type(e).__name__


def _is_writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".healthcheck")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _count_files(path: str, suffixes: tuple[str, ...] | None = None) -> int:
    if not os.path.isdir(path):
        return 0
    total = 0
    for _, _, files in os.walk(path):
        for filename in files:
            if suffixes and not filename.lower().endswith(suffixes):
                continue
            total += 1
    return total


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.6):
            return True
    except Exception:
        return False


def _safe_proxy_label(parsed) -> str:
    host = parsed.hostname or ""
    if not host:
        return ""
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 1080 if parsed.scheme.startswith("socks") else 80
    return f"{parsed.scheme}://{host}:{port}"


def _proxy_config_item(app_config: dict) -> dict:
    raw_proxy = str(global_config.proxy_url or app_config.get("proxy_url") or app_config.get("network_http_proxy") or "").strip()
    if not raw_proxy:
        return _item("proxy", "代理配置", "disabled", "未配置网络代理", "fa-route")

    parsed = urlparse(raw_proxy)
    if parsed.scheme not in {"http", "https", "socks4", "socks5", "socks5h"} or not parsed.hostname:
        return _item("proxy", "代理配置", "error", "代理地址格式无效", "fa-route", "请使用 http://host:port 或 socks5://host:port")

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 1080 if parsed.scheme.startswith("socks") else 80

    label = _safe_proxy_label(parsed)
    try:
        with socket.create_connection((parsed.hostname, int(port)), timeout=0.8):
            pass
        return _item("proxy", "代理配置", "ok", "代理端口可连接", "fa-route", label)
    except Exception as e:
        return _item("proxy", "代理配置", "error", f"代理端口不可连接: {str(e) or type(e).__name__}", "fa-route", label)


def _item(item_id: str, label: str, status: str, message: str, icon: str, detail: str = "") -> dict:
    return {
        "id": item_id,
        "label": label,
        "status": status,
        "message": message,
        "icon": icon,
        "detail": detail,
    }


def _summarize(items: list[dict]) -> dict:
    summary = {"total": len(items), "ok": 0, "warning": 0, "error": 0, "disabled": 0}
    for item in items:
        status = item.get("status")
        if status in summary:
            summary[status] += 1
    return summary


def _overall_status(summary: dict) -> str:
    if summary.get("error", 0) > 0:
        return "error"
    if summary.get("warning", 0) > 0:
        return "warning"
    return "ok"


def _normalize_url_origin(raw_url: str) -> str:
    text = str(raw_url or "").strip()
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = f"http://{text}"
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    return urlunparse((parsed.scheme or "http", parsed.netloc, "/", "", "", ""))


def _target_from_url(
    item_id: str,
    label: str,
    raw_url: str,
    group: str,
    icon: str,
    use_proxy: bool = False,
    source: str = "",
) -> dict | None:
    text = str(raw_url or "").strip()
    if not text:
        return None
    if not text.startswith(("http://", "https://")):
        text = f"https://{text}"
    parsed = urlparse(text)
    if not parsed.netloc:
        return None
    hostname = (parsed.hostname or "").lower()
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    url = urlunparse((parsed.scheme or "https", parsed.netloc, path, "", parsed.query, ""))
    return {
        "id": item_id,
        "label": label,
        "host": host,
        "hostname": hostname,
        "url": url,
        "group": group,
        "source": source or group,
        "icon": icon,
        "use_proxy": bool(use_proxy),
    }


def _safe_target_id(prefix: str, host: str) -> str:
    normalized = "".join(ch if ch.isalnum() else "_" for ch in host.lower()).strip("_")
    return f"{prefix}_{normalized}"[:96] or prefix


def _is_private_hostname(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return True
    try:
        addr = ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False


def _add_network_target(targets: list[dict], seen_hosts: set[str], target: dict | None) -> None:
    if not target:
        return
    host = str(target.get("host") or "").strip().lower()
    if not host or host in seen_hosts:
        return
    target["private"] = _is_private_hostname(str(target.get("hostname") or ""))
    targets.append(target)
    seen_hosts.add(host)


def _load_network_targets() -> list[dict]:
    targets: list[dict] = []
    seen_hosts: set[str] = set()

    for item_id, label, url, group, icon, use_proxy in STATIC_NETWORK_TARGETS:
        _add_network_target(targets, seen_hosts, _target_from_url(item_id, label, url, group, icon, use_proxy))

    app_config = _read_json(CONFIG_FILE, {})
    tmdb_base_url = str(app_config.get("tmdb_api_base_url") or "").strip()
    if tmdb_base_url:
        origin = _normalize_url_origin(tmdb_base_url)
        if origin:
            parsed = urlparse(origin)
            _add_network_target(
                targets,
                seen_hosts,
                _target_from_url(_safe_target_id("tmdb_custom", parsed.netloc), "TMDB 自定义 API", origin, "元数据", "fa-solid fa-film", True),
            )

    cfg302 = get_config_302_sync()
    for idx, emby in enumerate(cfg302.get("embys") or []):
        if not isinstance(emby, dict) or not emby.get("enabled", True):
            continue
        name = str(emby.get("name") or f"Emby {idx + 1}").strip()
        for key, label_suffix in (("url", "内网地址"), ("public_host", "公网地址")):
            origin = _normalize_url_origin(emby.get(key) or "")
            if not origin:
                continue
            parsed = urlparse(origin)
            _add_network_target(
                targets,
                seen_hosts,
                _target_from_url(
                    _safe_target_id(f"emby_{idx + 1}_{key}", parsed.netloc),
                    f"{name} {label_suffix}",
                    origin,
                    "Emby",
                    "fa-solid fa-server",
                    False,
                    "配置",
                ),
            )

    rss_tasks = _read_json(RSS_TASKS_FILE, [])
    for idx, task in enumerate(rss_tasks if isinstance(rss_tasks, list) else []):
        if not isinstance(task, dict) or not task.get("enabled", True):
            continue
        origin = _normalize_url_origin(task.get("rss_url") or task.get("feed_url") or task.get("url") or "")
        if not origin:
            continue
        parsed = urlparse(origin)
        name = str(task.get("name") or f"RSS {idx + 1}").strip()
        _add_network_target(
            targets,
            seen_hosts,
            _target_from_url(_safe_target_id("rss", parsed.netloc), f"{name} RSS", origin, "RSS", "fa-solid fa-rss", True, "配置"),
        )

    moviepilot_config = _read_json(os.path.join(os.path.dirname(CONFIG_FILE) or "config", "moviepilot.json"), {})
    mp_origin = _normalize_url_origin(moviepilot_config.get("mp_url") or "")
    if mp_origin:
        parsed = urlparse(mp_origin)
        _add_network_target(
            targets,
            seen_hosts,
            _target_from_url(_safe_target_id("moviepilot", parsed.netloc), "MoviePilot", mp_origin, "MoviePilot", "fa-solid fa-plane", False, "配置"),
        )

    return targets


def _proxy_for_network_target(target: dict, app_config: dict) -> str:
    if target.get("private") or not target.get("use_proxy"):
        return ""
    proxy_url = str(global_config.proxy_url or app_config.get("proxy_url") or "").strip()
    if proxy_url.startswith(("http://", "https://")):
        return proxy_url
    return ""


def _format_network_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "连接超时"
    if isinstance(exc, httpx.ConnectError):
        return "无法建立连接"
    if isinstance(exc, httpx.ProxyError):
        return "代理连接失败"
    if isinstance(exc, httpx.UnsupportedProtocol):
        return "协议不受支持"
    return str(exc) or type(exc).__name__


def _check_network_target(target: dict, app_config: dict) -> dict:
    started_at = time.perf_counter()
    result = {key: value for key, value in target.items() if key not in {"use_proxy", "private"}}
    proxy_url = _proxy_for_network_target(target, app_config)
    result["proxy_enabled"] = bool(proxy_url)
    try:
        client_kwargs: dict[str, Any] = {
            "timeout": NETWORK_CHECK_TIMEOUT,
            "follow_redirects": True,
            "verify": False,
            "headers": {"User-Agent": NETWORK_USER_AGENT},
        }
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        with httpx.Client(**client_kwargs) as client:
            req = client.build_request("GET", str(target.get("url") or ""))
            resp = client.send(req, stream=True)
            status_code = int(resp.status_code)
            resp.close()
        result["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
        result["status_code"] = status_code
        if status_code < 500:
            result["status"] = "ok"
            result["message"] = f"已连通，HTTP {status_code}"
        else:
            result["status"] = "warning"
            result["message"] = f"服务返回 HTTP {status_code}"
    except Exception as e:
        result["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
        result["status_code"] = 0
        result["status"] = "error"
        result["message"] = _format_network_error(e)
    return result


@router.get("")
def get_system_health():
    started_at = time.time()
    checks: list[dict] = []

    checks.append(_item("api", "ChillPoster API", "ok", "管理端 API 响应正常", "fa-heart-pulse"))

    try:
        from app.services.task_service import task_service_instance

        scheduler = task_service_instance.scheduler
        scheduler_running = bool(getattr(scheduler, "running", False))
        jobs = scheduler.get_jobs() if scheduler_running else []
        checks.append(_item(
            "scheduler",
            "任务调度器",
            "ok" if scheduler_running else "error",
            f"运行中，已加载 {len(jobs)} 个任务" if scheduler_running else "调度器未运行",
            "fa-clock-rotate-left",
        ))
    except Exception as e:
        checks.append(_item("scheduler", "任务调度器", "error", str(e) or "读取失败", "fa-clock-rotate-left"))

    try:
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(os.getcwd())
        cpu = psutil.cpu_percent(interval=0.1)
        status = "warning" if max(float(memory.percent), float(disk.percent)) >= 90 else "ok"
        checks.append(_item(
            "resources",
            "主机资源",
            status,
            f"CPU {cpu:.1f}% / 内存 {memory.percent:.1f}% / 磁盘 {disk.percent:.1f}%",
            "fa-gauge-high",
        ))
    except Exception as e:
        checks.append(_item("resources", "主机资源", "warning", str(e) or "读取失败", "fa-gauge-high"))

    config_dir = os.path.dirname(CONFIG_FILE) or "config"
    config_dir_writable = _is_writable_dir(config_dir)
    checks.append(_item(
        "config_dir",
        "配置目录",
        "ok" if config_dir_writable else "error",
        f"{config_dir} 可写" if config_dir_writable else f"{config_dir} 不可写",
        "fa-folder-check",
    ))

    json_paths = [
        CONFIG_FILE,
        os.path.join(config_dir, "config_302.json"),
        TASKS_FILE,
        RSS_TASKS_FILE,
        os.path.join(config_dir, "media_organize.json"),
    ]
    broken_json = []
    present_json = 0
    for path in json_paths:
        valid, state = _json_file_state(path)
        if os.path.exists(path):
            present_json += 1
        if not valid:
            broken_json.append(f"{os.path.basename(path)}: {state}")
    checks.append(_item(
        "config_json",
        "配置文件",
        "error" if broken_json else "ok",
        "配置 JSON 均可读取" if not broken_json else "；".join(broken_json[:2]),
        "fa-file-shield",
        f"已存在 {present_json}/{len(json_paths)} 个配置文件",
    ))

    fonts_count = _count_files(FONTS_DIR, (".ttf", ".otf", ".ttc"))
    templates_count = _count_files(TEMPLATES_DIR, (".json", ".jpg", ".jpeg", ".png", ".webp"))
    layouts_count = _count_files(LAYOUTS_DIR, (".py",))
    checks.append(_item(
        "poster_assets",
        "封面资源",
        "ok" if fonts_count and templates_count and layouts_count else "warning",
        f"字体 {fonts_count} / 模板 {templates_count} / 布局 {layouts_count}",
        "fa-images",
    ))

    backups_count = len([name for name in os.listdir(BACKUPS_DIR) if os.path.isdir(os.path.join(BACKUPS_DIR, name))]) if os.path.isdir(BACKUPS_DIR) else 0
    checks.append(_item(
        "backups",
        "封面备份",
        "ok" if os.path.isdir(BACKUPS_DIR) else "disabled",
        f"已发现 {backups_count} 个备份套件" if os.path.isdir(BACKUPS_DIR) else "暂未创建备份目录",
        "fa-box-archive",
    ))

    cfg302 = get_config_302_sync()
    embys = [item for item in (cfg302.get("embys") or []) if isinstance(item, dict) and item.get("enabled", True)]
    primary_emby = embys[0] if embys else {}
    if not primary_emby or not primary_emby.get("url") or not primary_emby.get("key"):
        checks.append(_item("emby", "Emby 服务", "disabled", "未配置 Emby 地址或 API Key", "fa-server"))
    else:
        client = EmbyClient(primary_emby.get("url", ""), primary_emby.get("key", ""), primary_emby.get("public_host") or None)
        try:
            info = client._request("GET", "emby/System/Info", timeout=5)
            server_name = (info or {}).get("ServerName") or (info or {}).get("Name") or primary_emby.get("name") or "Emby"
            checks.append(_item("emby", "Emby 服务", "ok", f"{server_name} 连接正常", "fa-server"))
        except Exception as e:
            checks.append(_item("emby", "Emby 服务", "error", f"无法连接: {str(e) or type(e).__name__}", "fa-server"))
        finally:
            client.close()

    gateway_ports = []
    for emby in embys:
        port = str(emby.get("proxy_port") or "").strip()
        if port.isdigit():
            gateway_ports.append(int(port))
    if not gateway_ports:
        checks.append(_item("gateway", "302 网关", "disabled", "未配置网关端口", "fa-network-wired"))
    else:
        open_ports = [port for port in gateway_ports if _port_open(port)]
        checks.append(_item(
            "gateway",
            "302 网关",
            "ok" if open_ports else "error",
            f"端口 {', '.join(map(str, open_ports))} 正在监听" if open_ports else f"端口 {', '.join(map(str, gateway_ports))} 未监听",
            "fa-network-wired",
        ))

    drives = [item for item in (cfg302.get("drives") or []) if isinstance(item, dict)]
    primary_drive = drives[0] if drives else {}
    drive_cookie_configured = bool(str(primary_drive.get("cookie") or "").strip())
    checks.append(_item(
        "drive115",
        "115 配置",
        "ok" if drive_cookie_configured else "disabled",
        (primary_drive.get("name") or "已配置 115 Cookie") if drive_cookie_configured else "未配置 115 Cookie",
        "fa-cloud",
    ))

    global_config.load()
    app_config = _read_json(CONFIG_FILE, {})
    checks.append(_item(
        "tmdb",
        "TMDB 配置",
        "ok" if str(app_config.get("tmdb_key") or "").strip() else "disabled",
        "API Key 已配置" if str(app_config.get("tmdb_key") or "").strip() else "未配置 API Key",
        "fa-database",
    ))
    checks.append(_proxy_config_item(app_config))

    rss_tasks = _read_json(RSS_TASKS_FILE, [])
    enabled_rss = len([task for task in rss_tasks if isinstance(task, dict) and task.get("enabled", True)])
    checks.append(_item(
        "rss",
        "RSS 真实库",
        "ok" if enabled_rss else "disabled",
        f"{enabled_rss} 个订阅任务已启用" if enabled_rss else "暂无启用的 RSS 任务",
        "fa-rss",
    ))

    webhook_config = _read_json(os.path.join(config_dir, "webhook.json"), {})
    checks.append(_item(
        "webhook",
        "Webhook",
        "ok" if webhook_config.get("enabled") else "disabled",
        "已启用" if webhook_config.get("enabled") else "未启用",
        "fa-bolt-lightning",
    ))

    try:
        from app.services.wechat_service import wechat_notify_service
        from app.services.telegram_service import telegram_notify_service

        wechat_cfg = wechat_notify_service.get_config()
        telegram_cfg = telegram_notify_service.get_config()
        notify_enabled = bool(wechat_cfg.get("enabled") or telegram_cfg.get("enabled") or telegram_cfg.get("account_monitor_enabled"))
        notify_detail = []
        if wechat_cfg.get("enabled"):
            notify_detail.append("企业微信")
        if telegram_cfg.get("enabled"):
            notify_detail.append("Telegram Bot")
        if telegram_cfg.get("account_monitor_enabled"):
            notify_detail.append("Telegram 监听")
        checks.append(_item(
            "notifications",
            "通知服务",
            "ok" if notify_enabled else "disabled",
            " / ".join(notify_detail) if notify_detail else "未启用通知通道",
            "fa-bell",
        ))
    except Exception as e:
        checks.append(_item("notifications", "通知服务", "warning", str(e) or "读取失败", "fa-bell"))

    docker_sock = "/var/run/docker.sock"
    checks.append(_item(
        "docker",
        "Docker Socket",
        "ok" if os.path.exists(docker_sock) else "disabled",
        "可访问 Docker Socket" if os.path.exists(docker_sock) else "未挂载 Docker Socket",
        "fa-cubes",
    ))

    summary = _summarize(checks)
    return {
        "status": _overall_status(summary),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_ms": int((time.time() - started_at) * 1000),
        "summary": summary,
        "items": checks,
    }


@router.get("/network")
def get_network_connectivity(target_id: str = ""):
    started_at = time.time()
    global_config.load()
    app_config = _read_json(CONFIG_FILE, {})
    targets = _load_network_targets()
    selected_id = str(target_id or "").strip()
    if selected_id:
        targets = [target for target in targets if target.get("id") == selected_id]

    if selected_id and not targets:
        items = [{
            "id": selected_id,
            "label": "网络检测目标",
            "host": selected_id,
            "url": "",
            "group": "网络",
            "source": "配置",
            "icon": "fa-solid fa-circle-question",
            "status": "error",
            "message": "未找到检测目标",
            "latency_ms": 0,
            "status_code": 0,
            "proxy_enabled": False,
        }]
    elif not targets:
        items = []
    else:
        items = []
        max_workers = min(NETWORK_CHECK_WORKERS, max(len(targets), 1))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(_check_network_target, target, app_config): target for target in targets}
            for future in as_completed(future_map):
                try:
                    items.append(future.result())
                except Exception as e:
                    target = future_map[future]
                    items.append({
                        "id": target.get("id"),
                        "label": target.get("label"),
                        "host": target.get("host"),
                        "url": target.get("url"),
                        "group": target.get("group"),
                        "source": target.get("source"),
                        "icon": target.get("icon") or "fa-solid fa-globe",
                        "status": "error",
                        "message": _format_network_error(e),
                        "latency_ms": 0,
                        "status_code": 0,
                        "proxy_enabled": False,
                    })
        order_map = {target.get("id"): idx for idx, target in enumerate(targets)}
        items.sort(key=lambda item: order_map.get(item.get("id"), 9999))

    summary = _summarize(items)
    return {
        "status": _overall_status(summary),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_ms": int((time.time() - started_at) * 1000),
        "summary": summary,
        "items": items,
    }
