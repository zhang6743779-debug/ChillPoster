import os
import re
import threading
import time
import uuid

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.dependencies import ACTIVE_TASKS, set_task_detail, update_task_progress
from app.services.docker_api import DockerAPI, get_current_container_id
from core.configs import global_config
from core.logger import logger


router = APIRouter(tags=["Upgrade"])

_UPGRADE_TASK_LOCK = threading.Lock()
_VERSION_CACHE = {"ts": 0.0, "latest": "", "message": ""}
_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+){1,4}$")


class UpgradeStartRequest(BaseModel):
    pass


class UpgradeCheckRequest(BaseModel):
    force: bool = True


def _project_version() -> str:
    version_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "VERSION")
    version = ""
    if os.path.exists(version_file):
        try:
            with open(version_file, "r", encoding="utf-8") as f:
                version = f.read().strip()
        except Exception:
            version = ""
    if not version:
        version = os.getenv("CHILLPOSTER_VERSION", "vdev").strip() or "vdev"
    return version if version.startswith("v") else f"v{version}"


def _config() -> dict:
    return {
        "enabled": True,
        "mode": "docker",
        "image": os.getenv("CHILLPOSTER_IMAGE", "chillne/chillposter:latest").strip() or "chillne/chillposter:latest",
        "timeout": int(os.getenv("CHILLPOSTER_UPGRADE_TIMEOUT", "600") or "600"),
    }


def _validate_image(image: str):
    if not image.startswith("chillne/chillposter"):
        raise RuntimeError("只允许升级 chillne/chillposter 镜像")
    if any(ch in image for ch in " ;|&`$<>\\"):
        raise RuntimeError("镜像名称包含非法字符")


def _check_docker_available() -> tuple[bool, str, str]:
    if not os.path.exists("/var/run/docker.sock"):
        return False, "", "未挂载 /var/run/docker.sock"
    try:
        api = DockerAPI(timeout=10)
        api.ping()
        container_id = get_current_container_id(api)
        if not container_id:
            return False, "", "无法识别当前容器"
        return True, container_id, ""
    except Exception as e:
        return False, "", str(e)


def _parse_version(value: str) -> tuple[int, ...] | None:
    text = str(value or "").strip().lstrip("v")
    if not text:
        return None
    parts = text.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _is_newer(latest: str, current: str) -> bool:
    latest_tuple = _parse_version(latest)
    current_tuple = _parse_version(current)
    if not latest_tuple or not current_tuple:
        return bool(latest and latest != current)
    max_len = max(len(latest_tuple), len(current_tuple))
    return latest_tuple + (0,) * (max_len - len(latest_tuple)) > current_tuple + (0,) * (max_len - len(current_tuple))


def _normalize_version(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.startswith("v") else f"v{text}"


def _fetch_latest_version(force: bool = False) -> tuple[str, str]:
    now = time.time()
    if not force and _VERSION_CACHE["latest"] and now - _VERSION_CACHE["ts"] < 1800:
        return _VERSION_CACHE["latest"], _VERSION_CACHE["message"]
    try:
        url = "https://hub.docker.com/v2/repositories/chillne/chillposter/tags?page_size=100"
        global_config.load()
        proxy_url = str(global_config.proxy_url or "").strip()
        client_kwargs = {"timeout": 8.0, "follow_redirects": True}
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
        versions = []
        for item in data.get("results") or []:
            name = str(item.get("name") or "").strip()
            if _VERSION_RE.match(name):
                versions.append(_normalize_version(name))
        versions.sort(key=lambda value: _parse_version(value) or (0,), reverse=True)
        latest = versions[0] if versions else ""
        message = "" if latest else "未找到版本标签"
    except Exception as e:
        latest = ""
        message = f"无法检查最新版本: {e}"
    _VERSION_CACHE.update({"ts": now, "latest": latest, "message": message})
    return latest, message


def _status(force_version: bool = False) -> dict:
    cfg = _config()
    current = _project_version()
    latest, version_message = _fetch_latest_version(force=force_version)
    docker_ok, container_id, docker_message = _check_docker_available()
    messages = [msg for msg in [version_message, docker_message if not docker_ok else ""] if msg]
    return {
        "enabled": True,
        "available": docker_ok,
        "mode": "docker",
        "selected_mode": "docker",
        "current_version": current,
        "latest_version": latest,
        "update_available": _is_newer(latest, current) if latest else False,
        "image": cfg["image"],
        "docker_available": docker_ok,
        "container_id": container_id[:12] if container_id else "",
        "message": "；".join(messages),
    }


def _has_running_upgrade() -> bool:
    return any(str(run_id).startswith("upgrade_") and task.get("status") == "running" for run_id, task in ACTIVE_TASKS.items())


def _set_detail(run_id: str, **detail):
    task = ACTIVE_TASKS.get(run_id, {})
    existing = task.get("detail") if isinstance(task.get("detail"), dict) else {}
    existing.update(detail)
    set_task_detail(run_id, existing)


def _run_docker_upgrade(run_id: str, cfg: dict):
    _validate_image(cfg["image"])
    update_task_progress(run_id, "系统升级", 10, "running")
    _set_detail(run_id, step="validate", message="正在检查 Docker Socket")
    docker_ok, container_id, docker_message = _check_docker_available()
    if not docker_ok:
        raise RuntimeError(docker_message)

    api = DockerAPI(timeout=cfg["timeout"])
    info = api.inspect_container(container_id)
    update_task_progress(run_id, "系统升级", 35, "running")
    _set_detail(run_id, step="pull", message=f"正在拉取镜像: {cfg['image']}")
    api.pull_image(cfg["image"])

    helper_name = f"chillposter-upgrade-{uuid.uuid4().hex[:8]}"
    socket_bind = "/var/run/docker.sock:/var/run/docker.sock"
    helper_code = (
        "import sys, time; "
        "from app.services.self_upgrade_helper import replace_container; "
        "time.sleep(2); "
        "replace_container(sys.argv[1], sys.argv[2], skip_pull=True)"
    )
    cmd = [
        "python", "-c", helper_code,
        container_id,
        cfg["image"],
    ]
    payload = {
        "Image": cfg["image"],
        "Cmd": cmd,
        "Env": ["PYTHONUNBUFFERED=1"],
        "HostConfig": {
            "Binds": [socket_bind],
            "AutoRemove": True,
            "NetworkMode": "bridge",
        },
        "Labels": {
            "chillposter.upgrade.helper": "true",
            "chillposter.upgrade.target": container_id,
        },
    }

    update_task_progress(run_id, "系统升级", 75, "running")
    _set_detail(run_id, step="helper", message="正在启动升级助手，随后服务会重启")
    created = api.create_container(helper_name, payload)
    helper_id = str((created or {}).get("Id") or "")
    if not helper_id:
        raise RuntimeError(f"升级助手创建失败: {created}")
    api.start_container(helper_id)
    update_task_progress(run_id, "系统升级", 90, "running")
    _set_detail(run_id, step="restarting", message="升级助手已启动，正在等待服务重启")


def _upgrade_worker(run_id: str, cfg: dict):
    try:
        update_task_progress(run_id, "系统升级", 5, "running")
        _set_detail(run_id, step="start", mode="docker", image=cfg["image"], message="升级任务已启动")
        _run_docker_upgrade(run_id, cfg)
    except Exception as e:
        logger.error(f"[Upgrade] 升级失败: {e}", exc_info=True)
        update_task_progress(run_id, "系统升级", 100, "error")
        _set_detail(run_id, step="error", message=str(e))


@router.get("/api/upgrade/status")
def upgrade_status():
    return _status(force_version=False)


@router.post("/api/upgrade/check")
def upgrade_check(_: UpgradeCheckRequest = UpgradeCheckRequest()):
    return _status(force_version=True)


@router.post("/api/upgrade/start")
def upgrade_start(_: UpgradeStartRequest = UpgradeStartRequest()):
    cfg = _config()
    _validate_image(cfg["image"])

    with _UPGRADE_TASK_LOCK:
        if _has_running_upgrade():
            raise HTTPException(status_code=409, detail="已有升级任务正在运行")
        run_id = f"upgrade_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        update_task_progress(run_id, "系统升级", 0, "running")
        _set_detail(run_id, mode="docker", image=cfg["image"])
        threading.Thread(target=_upgrade_worker, args=(run_id, cfg), daemon=True).start()
    return {"status": "ok", "run_id": run_id, "message": "升级任务已启动"}
