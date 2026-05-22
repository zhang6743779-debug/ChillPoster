import os
import json
import uuid
import random
import threading
import socket
import ipaddress
import psutil
from fastapi import APIRouter, HTTPException
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel
from typing import List
from p115client import P115Client
from core.logger import logger
from core.media_library_cache import build_task_key, prune_tasks_by_keys

router = APIRouter(prefix="/api/strm", tags=["strm"])

CONFIG_FILE = "config/strm_config.json"
DAILY_FULL_SYNC_JOB_ID = "strm_daily_full_sync_0400"


# ==========================================
# 1. 数据模型
# ==========================================

class StrmSyncTask(BaseModel):
    name: str = '标准媒体库同步'
    drive_index: int = 0
    remote_path: str = ''
    local_path: str = ''
    download_auxiliary: bool = True
    download_tmdb_metadata: bool = False
    strm_url_base: str = ''
    min_video_size_mb: int = 0
    video_exts_str: str = '.mp4,.mpg,.mkv,.mpeg,.ts,.vob,.iso,.m4v,.avi,.3gp,.wmv,.webm,.flv,.mov,.m2ts,.rmvb,.rm,.asf,.f4v,.m2t,.mts,.mpe,.tp,.trp,.divx,.ogv,.dv'
    audio_exts_str: str = '.mp3,.flac,.wav,.m4a,.ape,.dsd,.dff,.dsf,.ac3,.dts'
    image_exts_str: str = '.jpg,.jpeg,.png,.webp,.bmp,.tiff,.tif,.ico,.gif,.svg,.heic,.avif,.raw'
    data_exts_str: str = '.nfo,.lrc,.srt,.pdf,.ass,.ssa,.md,.sub,.sup,.idx,.txt,.xml,.json,.smi,.vtt,.ttml,.dfxp,.scc,.bup,.ifo'
    poll_interval: int = 60
    overwrite: str = 'skip'
    aux_download_mode: str = 'cdn'  # cdn 或 standard

    class Config:
        extra = "ignore"


class StrmConfigPayload(BaseModel):
    sync_tasks: List[StrmSyncTask] = []

    class Config:
        extra = "ignore"


class StrmStartPayload(BaseModel):
    task_index: int = 0
    mode: str = 'full'


class StrmStopPayload(BaseModel):
    run_id: str = ''


class BrowsePayload(BaseModel):
    cid: str = '0'
    drive_index: int = 0


class LocalBrowsePayload(BaseModel):
    path: str = '/'


# ==========================================
# 2. 路由逻辑
# ==========================================

def _iter_lan_ipv4_candidates() -> list[tuple[int, str, str]]:
    interface_priority = [
        ("en", 0),
        ("eth", 1),
        ("wlan", 2),
        ("wl", 3),
        ("bridge", 20),
        ("br-", 21),
        ("docker", 30),
        ("veth", 31),
        ("utun", 40),
        ("lo", 90),
    ]
    ip_priority = [
        (ipaddress.ip_network("192.168.0.0/16"), 0),
        (ipaddress.ip_network("10.0.0.0/8"), 1),
        (ipaddress.ip_network("172.16.0.0/12"), 2),
    ]
    candidates: list[tuple[int, str, str]] = []
    try:
        for ifname, addrs in psutil.net_if_addrs().items():
            if_lower = str(ifname or "").lower()
            if_priority = 10
            for prefix, priority in interface_priority:
                if if_lower.startswith(prefix):
                    if_priority = priority
                    break
            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                ip = str(addr.address or "").strip()
                if not ip:
                    continue
                try:
                    ip_obj = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_unspecified:
                    continue
                ip_score = 50
                for network, priority in ip_priority:
                    if ip_obj in network:
                        ip_score = priority
                        break
                candidates.append((ip_score * 100 + if_priority, if_lower, ip))
    except Exception:
        return []
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates


def select_lan_ipv4() -> str:
    candidates = _iter_lan_ipv4_candidates()
    return candidates[0][2] if candidates else ""


def derive_strm_url_base(config_302_data: dict, drive_index: int) -> str:
    embys = config_302_data.get("embys", []) if isinstance(config_302_data, dict) else []
    configured_host = str(os.environ.get("CHILLPOSTER_STRM_HOST") or "").strip().rstrip("/")
    if configured_host.startswith("http://") or configured_host.startswith("https://"):
        host_base = configured_host
    elif configured_host:
        host_base = f"http://{configured_host}"
    else:
        lan_ip = select_lan_ipv4()
        host_base = f"http://{lan_ip}" if lan_ip else ""
    if not host_base:
        return ""
    for emby in embys:
        if not isinstance(emby, dict):
            continue
        if not emby.get("enabled", True):
            continue
        proxy_port = str(emby.get("proxy_port") or "").strip()
        if proxy_port:
            return f"{host_base}:{proxy_port}"
    return ""


def hydrate_strm_task(task: dict, config_302_data: dict | None = None) -> dict:
    hydrated = dict(task or {})
    try:
        drive_index = int(hydrated.get("drive_index", 0) or 0)
    except (TypeError, ValueError):
        drive_index = 0
    if config_302_data is None:
        from app.routers.config_302 import get_config_302_sync
        config_302_data = get_config_302_sync()
    hydrated["strm_url_base"] = derive_strm_url_base(config_302_data, drive_index)
    return hydrated


def _has_running_strm_or_organize_task() -> str:
    try:
        from app.dependencies import ACTIVE_TASKS
        for task in ACTIVE_TASKS.values():
            if not isinstance(task, dict):
                continue
            if str(task.get("status", "") or "") == "running":
                task_type = str(task.get("task_type", "") or "")
                if task_type == "strm":
                    return "已有 STRM 同步任务运行中"
                if task_type == "media_organize":
                    return "已有媒体整理任务运行中"
    except Exception:
        return ""
    return ""


def _run_daily_full_sync_sequence(tasks: list[dict]):
    from app.dependencies import update_task_progress
    from app.services.strm_service import strm_service

    total = len(tasks)
    for idx, task_config in enumerate(tasks, 1):
        run_id = f"strm_daily_{uuid.uuid4().hex[:8]}"
        task_name = str(task_config.get("name", "") or f"任务{idx}")
        update_task_progress(run_id, f"STRM定时全量同步: {task_name}", 0, "running")
        logger.info(f"[STRM] 定时全量同步开始: {idx}/{total} {task_name} (run_id={run_id})")
        try:
            strm_service.run_full_sync(task_config, run_id)
        except Exception as e:
            logger.error(f"[STRM] 定时全量同步异常: {task_name}: {e}", exc_info=True)
            update_task_progress(run_id, f"STRM定时全量同步失败: {task_name}", 100, "error")


def run_daily_full_sync_job():
    from app.services.strm_service import strm_service

    running_reason = _has_running_strm_or_organize_task()
    if running_reason:
        logger.warning(f"[STRM] 定时全量同步跳过: {running_reason}")
        return

    config = strm_service.load_config()
    tasks = [task for task in config.get("sync_tasks", []) if isinstance(task, dict)]
    tasks = [
        task for task in tasks
        if str(task.get("remote_path", "") or "").strip()
        and str(task.get("local_path", "") or "").strip()
    ]
    if not tasks:
        default_task = _build_standard_topology_default_task()
        if default_task:
            tasks = [default_task]
    if not tasks:
        logger.warning("[STRM] 定时全量同步跳过: 没有可用的 STRM 同步配置")
        return

    logger.info(f"[STRM] 定时全量同步已提交: {len(tasks)} 个任务")
    strm_service._executor.submit(_run_daily_full_sync_sequence, tasks)


def schedule_daily_full_sync_job(scheduler):
    try:
        def delayed_job():
            delay_seconds = random.randint(0, 3600)
            logger.info(f"[STRM] 每日全量同步随机延迟: {delay_seconds}s")
            timer = threading.Timer(delay_seconds, run_daily_full_sync_job)
            timer.daemon = True
            timer.start()

        scheduler.add_job(
            delayed_job,
            CronTrigger.from_crontab("0 4 * * *"),
            id=DAILY_FULL_SYNC_JOB_ID,
            name="STRM每日全量同步",
            replace_existing=True,
        )
        logger.info("[STRM] 已注册每日全量同步任务: 04:00-05:00 随机启动")
    except Exception as e:
        logger.error(f"[STRM] 注册每日全量同步任务失败: {e}")


def _build_standard_topology_default_task() -> dict | None:
    try:
        from app.routers.config_302 import get_config_302_sync

        cfg302 = get_config_302_sync()
        topology = cfg302.get("standard_topology") if isinstance(cfg302, dict) else None
        if not isinstance(topology, dict):
            return None

        drives = cfg302.get("drives", []) if isinstance(cfg302.get("drives"), list) else []
        drive = drives[0] if drives else {}
        drive_index = 0

        task = StrmSyncTask().dict()
        task.update({
            "name": "标准媒体库同步",
            "drive_index": drive_index,
            "remote_path": topology.get("media_dir", ""),
            "local_path": topology.get("local_media_dir", ""),
        })
        task = hydrate_strm_task(task, cfg302)
        return task if task["remote_path"] and task["local_path"] else None
    except Exception:
        return None


@router.get("/get")
async def get_strm_config():
    """读取 STRM 配置"""
    if not os.path.exists(CONFIG_FILE):
        default_task = _build_standard_topology_default_task()
        return {"sync_tasks": [default_task] if default_task else []}
    try:
        from app.routers.config_302 import get_config_302_sync
        cfg302 = get_config_302_sync()
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {"sync_tasks": []}
        if not isinstance(data.get("sync_tasks"), list):
            data["sync_tasks"] = []
        if not data["sync_tasks"]:
            default_task = _build_standard_topology_default_task()
            if default_task:
                data["sync_tasks"] = [default_task]
        else:
            data["sync_tasks"] = [hydrate_strm_task(task, cfg302) for task in data["sync_tasks"] if isinstance(task, dict)]
        return data
    except Exception as e:
        logger.error(f"[STRM] 读取配置失败: {e}")
        default_task = _build_standard_topology_default_task()
        return {"sync_tasks": [default_task] if default_task else []}


@router.post("/save")
async def save_strm_config(config: StrmConfigPayload):
    """保存 STRM 配置"""
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        save_data = {"sync_tasks": []}
        for task in config.sync_tasks:
            task_data = task.dict()
            task_data.pop("strm_url_base", None)
            save_data["sync_tasks"].append(task_data)

        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(save_data, f, ensure_ascii=False, indent=4)

        valid_task_keys = set()
        for task in config.sync_tasks:
            remote_path = str(task.remote_path or "").rstrip("/")
            if not remote_path:
                continue
            valid_task_keys.add(build_task_key(task.drive_index, remote_path))
        removed_count = prune_tasks_by_keys(valid_task_keys)

        logger.info(f"[STRM] 配置已保存，共 {len(config.sync_tasks)} 个同步任务，已清理 {removed_count} 个旧缓存任务")
        return {"status": "success", "message": "STRM 配置已保存"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")


@router.post("/start")
async def start_strm_sync(payload: StrmStartPayload):
    """启动同步任务（全量或增量+监控）"""
    from app.services.strm_service import strm_service

    config = strm_service.load_config()
    tasks = config.get("sync_tasks", [])

    if payload.task_index < 0 or payload.task_index >= len(tasks):
        return {"status": "error", "message": "任务索引无效"}

    task_config = tasks[payload.task_index]
    run_id = uuid.uuid4().hex[:8]
    mode = payload.mode

    # 初始化进度
    from app.dependencies import update_task_progress
    update_task_progress(run_id, f"STRM{mode}同步", 0, "running")

    if mode == "full":
        strm_service._executor.submit(strm_service.run_full_sync, task_config, run_id)
    else:
        return {"status": "error", "message": f"不支持的模式: {mode}"}

    mode_label = "全量"
    logger.info(f"[STRM] 已启动 {mode_label} 同步: {task_config.get('name', '')} (run_id={run_id})")
    return {"status": "ok", "run_id": run_id, "message": f"已启动 {mode_label} 同步"}


@router.post("/stop")
async def stop_strm_sync(payload: StrmStopPayload):
    """取消同步/监控任务"""
    from app.dependencies import request_task_cancel
    task = request_task_cancel(payload.run_id)
    if task and task.get("status") not in ("finished", "error", "stopped", "interrupted"):
        logger.info(f"[STRM] 已请求取消任务: {payload.run_id}")
        return {"status": "ok", "message": "已发送取消请求"}
    return {"status": "error", "message": "任务不存在或已完成"}


@router.get("/progress")
async def get_strm_progress():
    """获取 STRM 相关的同步进度"""
    from app.dependencies import ACTIVE_TASKS

    strm_tasks = {}
    for run_id, task in ACTIVE_TASKS.items():
        name = task.get("name", "")
        if "STRM" in name:
            strm_tasks[run_id] = task

    return {"tasks": strm_tasks}


@router.post("/browse_local")
async def browse_local(payload: LocalBrowsePayload):
    """浏览本地目录（返回子目录列表）"""
    try:
        target = payload.path or "/"
        if not os.path.isdir(target):
            return {"status": "error", "message": "目录不存在", "dirs": []}

        dirs = []
        # 返回上级目录
        parent = os.path.dirname(target.rstrip("/"))
        if parent != target:
            dirs.append({"name": "..", "path": parent})

        for entry in sorted(os.listdir(target)):
            full = os.path.join(target, entry)
            if os.path.isdir(full) and not entry.startswith('.'):
                dirs.append({"name": entry, "path": full})

        return {"status": "ok", "dirs": dirs, "current": target}
    except PermissionError:
        return {"status": "error", "message": "无权限访问", "dirs": []}
    except Exception as e:
        return {"status": "error", "message": f"浏览失败: {str(e)}", "dirs": []}


@router.post("/browse115")
async def browse_115(payload: BrowsePayload):
    """浏览 115 目录（返回子目录列表）"""
    try:
        cfg_path = "config/config_302.json"
        if not os.path.exists(cfg_path):
            return {"status": "error", "message": "302 配置不存在", "dirs": []}

        with open(cfg_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)

        drives = cfg.get("drives", [])
        drive_cfg = drives[0] if isinstance(drives, list) and drives else cfg.get("drive", {})
        if not isinstance(drive_cfg, dict) or not drive_cfg:
            return {"status": "error", "message": "未配置 115 账号", "dirs": []}

        cookie = str(drive_cfg.get("cookie", "") or "").strip()
        if not cookie:
            return {"status": "error", "message": "Cookie 未配置", "dirs": []}

        client = P115Client(cookie)
        cid = payload.cid or "0"

        resp = client.fs_files_app(
            {"cid": int(cid), "limit": 1150, "fc_mix": 0},
            app="android",
            base_url="https://proapi.115.com",
            headers={"user-agent": "Mozilla/5.0 (Linux; Android 13; 23013RK75C Build/TKQ1.221114.001) AppleWebKit/537.36 Chrome/123.0.0.0 Mobile Safari/537.36"},
        )
        if not resp or not resp.get("state"):
            return {"status": "error", "message": "读取目录失败", "dirs": []}

        dirs = []
        for item in resp.get("data", []):
            if item.get("fc") == "0":
                dirs.append({
                    "name": item.get("fn", ""),
                    "cid": str(item.get("fid", ""))
                })

        return {"status": "ok", "dirs": dirs}

    except Exception as e:
        return {"status": "error", "message": f"浏览失败: {str(e)}", "dirs": []}
