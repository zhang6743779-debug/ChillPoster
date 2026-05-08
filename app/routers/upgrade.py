import hmac
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.dependencies import ACTIVE_TASKS, update_task_progress
from app.routers.auth import get_auth_creds
from app.services.docker_api import DockerAPI, get_current_container_id
from core.logger import logger


router = APIRouter(tags=["Upgrade"])

_UPGRADE_TASK_LOCK = threading.Lock()
_VERSION_CACHE = {"ts": 0.0, "latest": "", "message": ""}
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+){1,4}$")


class UpgradeStartRequest(BaseModel):
    password: str = ""
    confirm: str = ""
    mode: str = "auto"


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


def _upgrade_enabled() -> bool:
    return os.getenv("CHILLPOSTER_UPGRADE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _config() -> dict:
    return {
        "enabled": _upgrade_enabled(),
        "mode": os.getenv("CHILLPOSTER_UPGRADE_MODE", "auto").strip().lower() or "auto",
        "compose_file": os.getenv("CHILLPOSTER_COMPOSE_FILE", "").strip(),
        "compose_project": os.getenv("CHILLPOSTER_COMPOSE_PROJECT", "").strip(),
        "compose_service": os.getenv("CHILLPOSTER_COMPOSE_SERVICE", "chillposter").strip() or "chillposter",
        "image": os.getenv("CHILLPOSTER_IMAGE", "chillne/chillposter:latest").strip() or "chillne/chillposter:latest",
        "docker_bin": os.getenv("CHILLPOSTER_DOCKER_BIN", "docker").strip() or "docker",
        "timeout": int(os.getenv("CHILLPOSTER_UPGRADE_TIMEOUT", "600") or "600"),
    }


def _normalize_mode(mode: str) -> str:
    value = str(mode or "auto").strip().lower()
    if value not in {"auto", "compose", "docker"}:
        raise HTTPException(status_code=400, detail="升级模式无效")
    return value


def _validate_name(value: str, label: str, required: bool = False):
    if not value:
        if required:
            raise RuntimeError(f"{label} 未配置")
        return
    if not _VALID_NAME_RE.match(value):
        raise RuntimeError(f"{label} 只能包含字母、数字、点、下划线和短横线")


def _validate_image(image: str):
    if not image.startswith("chillne/chillposter"):
        raise RuntimeError("只允许升级 chillne/chillposter 镜像")
    if any(ch in image for ch in " ;|&`$<>\\"):
        raise RuntimeError("镜像名称包含非法字符")


def _compose_base_cmd(cfg: dict) -> list[str]:
    compose_file = cfg["compose_file"]
    if not compose_file:
        raise RuntimeError("未配置 CHILLPOSTER_COMPOSE_FILE")
    if not os.path.isabs(compose_file) or not os.path.exists(compose_file):
        raise RuntimeError("Compose 文件不存在或不是绝对路径")
    _validate_name(cfg["compose_project"], "Compose project")
    _validate_name(cfg["compose_service"], "Compose service", required=True)
    cmd = [cfg["docker_bin"], "compose", "-f", compose_file]
    if cfg["compose_project"]:
        cmd.extend(["-p", cfg["compose_project"]])
    return cmd


def _run_cmd(cmd: list[str], timeout: int, log_level: str = "info") -> subprocess.CompletedProcess:
    log_message = f"[Upgrade] 执行命令: {' '.join(cmd)}"
    if log_level == "debug":
        logger.debug(log_message)
    else:
        logger.info(log_message)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)


def _check_compose_available(cfg: dict) -> tuple[bool, str]:
    if not shutil.which(cfg["docker_bin"]):
        return False, "容器内未找到 docker 命令"
    if not cfg["compose_file"]:
        return False, "未配置 Compose 文件"
    try:
        cmd = _compose_base_cmd(cfg) + ["version"]
        result = _run_cmd(cmd, 20, log_level="debug")
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "docker compose 不可用").strip()
        services = _run_cmd(_compose_base_cmd(cfg) + ["config", "--services"], 30, log_level="debug")
        if services.returncode != 0:
            return False, (services.stderr or services.stdout or "无法读取 Compose 服务").strip()
        service_set = {line.strip() for line in services.stdout.splitlines() if line.strip()}
        if cfg["compose_service"] not in service_set:
            return False, f"Compose 服务不存在: {cfg['compose_service']}"
        return True, ""
    except Exception as e:
        return False, str(e)


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
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
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


def _select_mode(requested_mode: str, cfg: dict, compose_ok: bool, docker_ok: bool) -> tuple[str, str]:
    mode = _normalize_mode(requested_mode or cfg["mode"])
    if mode == "compose":
        return ("compose", "") if compose_ok else ("", "Compose 模式不可用")
    if mode == "docker":
        return ("docker", "") if docker_ok else ("", "Docker 直接模式不可用")
    if compose_ok:
        return "compose", ""
    if docker_ok:
        return "docker", ""
    return "", "没有可用的升级模式"


def _status(force_version: bool = False) -> dict:
    cfg = _config()
    current = _project_version()
    if cfg["enabled"]:
        latest, version_message = _fetch_latest_version(force=force_version)
    else:
        latest, version_message = "", ""
    compose_ok, compose_message = _check_compose_available(cfg) if cfg["enabled"] else (False, "升级未启用")
    docker_ok, container_id, docker_message = _check_docker_available() if cfg["enabled"] else (False, "", "升级未启用")
    selected_mode, mode_message = _select_mode(cfg["mode"], cfg, compose_ok, docker_ok) if cfg["enabled"] else ("", "升级未启用")
    messages = [msg for msg in [mode_message, version_message, compose_message if cfg["mode"] == "compose" else "", docker_message if cfg["mode"] == "docker" else ""] if msg]
    return {
        "enabled": cfg["enabled"],
        "available": bool(cfg["enabled"] and selected_mode),
        "mode": cfg["mode"],
        "selected_mode": selected_mode,
        "current_version": current,
        "latest_version": latest,
        "update_available": _is_newer(latest, current) if latest else False,
        "image": cfg["image"],
        "compose_available": compose_ok,
        "docker_available": docker_ok,
        "compose_service": cfg["compose_service"],
        "compose_file_configured": bool(cfg["compose_file"]),
        "container_id": container_id[:12] if container_id else "",
        "message": "；".join(messages),
    }


def _require_upgrade_password(password: str):
    creds = get_auth_creds()
    expected = str(creds.get("password", "password"))
    if not hmac.compare_digest(str(password or ""), expected):
        raise HTTPException(status_code=401, detail="管理员密码错误")


def _has_running_upgrade() -> bool:
    return any(str(run_id).startswith("upgrade_") and task.get("status") == "running" for run_id, task in ACTIVE_TASKS.items())


def _set_detail(run_id: str, **detail):
    task = ACTIVE_TASKS.setdefault(run_id, {})
    existing = task.get("detail") if isinstance(task.get("detail"), dict) else {}
    existing.update(detail)
    task["detail"] = existing


def _run_compose_upgrade(run_id: str, cfg: dict):
    update_task_progress(run_id, "系统升级", 10, "running")
    _set_detail(run_id, step="validate", message="正在检查 Docker Compose 配置")
    compose_ok, compose_message = _check_compose_available(cfg)
    if not compose_ok:
        raise RuntimeError(compose_message)

    base = _compose_base_cmd(cfg)
    update_task_progress(run_id, "系统升级", 35, "running")
    _set_detail(run_id, step="pull", message=f"正在拉取镜像: {cfg['image']}")
    pull = _run_cmd(base + ["pull", cfg["compose_service"]], cfg["timeout"])
    if pull.returncode != 0:
        raise RuntimeError((pull.stderr or pull.stdout or "镜像拉取失败").strip())

    update_task_progress(run_id, "系统升级", 85, "running")
    _set_detail(run_id, step="recreate", message="正在重建服务，页面可能会短暂断开")
    up = _run_cmd(base + ["up", "-d", "--no-deps", cfg["compose_service"]], cfg["timeout"])
    if up.returncode != 0:
        raise RuntimeError((up.stderr or up.stdout or "服务重建失败").strip())

    update_task_progress(run_id, "系统升级", 100, "finished")
    _set_detail(run_id, step="done", message="升级命令已完成")


def _run_docker_upgrade(run_id: str, cfg: dict):
    _validate_image(cfg["image"])
    update_task_progress(run_id, "系统升级", 10, "running")
    _set_detail(run_id, step="validate", message="正在检查 Docker Socket")
    docker_ok, container_id, docker_message = _check_docker_available()
    if not docker_ok:
        raise RuntimeError(docker_message)

    api = DockerAPI(timeout=300)
    info = api.inspect_container(container_id)
    current_image = str((info.get("Config") or {}).get("Image") or "")
    update_task_progress(run_id, "系统升级", 35, "running")
    _set_detail(run_id, step="pull", message=f"正在拉取镜像: {cfg['image']}")
    api.pull_image(cfg["image"])

    helper_name = f"chillposter-upgrade-{uuid.uuid4().hex[:8]}"
    socket_bind = "/var/run/docker.sock:/var/run/docker.sock"
    cmd = [
        "python", "-m", "app.services.self_upgrade_helper",
        "--container-id", container_id,
        "--image", cfg["image"],
        "--delay", "2",
        "--skip-pull",
    ]
    payload = {
        "Image": current_image or cfg["image"],
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


def _upgrade_worker(run_id: str, mode: str, cfg: dict):
    try:
        update_task_progress(run_id, "系统升级", 5, "running")
        _set_detail(run_id, step="start", message="升级任务已启动")
        compose_ok, _ = _check_compose_available(cfg)
        docker_ok, _, _ = _check_docker_available()
        selected_mode, message = _select_mode(mode, cfg, compose_ok, docker_ok)
        if not selected_mode:
            raise RuntimeError(message)
        _set_detail(run_id, mode=selected_mode, image=cfg["image"])
        if selected_mode == "compose":
            _run_compose_upgrade(run_id, cfg)
        else:
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
def upgrade_start(req: UpgradeStartRequest):
    cfg = _config()
    if not cfg["enabled"]:
        raise HTTPException(status_code=400, detail="一键升级未启用，请设置 CHILLPOSTER_UPGRADE_ENABLED=true")
    _require_upgrade_password(req.password)
    if str(req.confirm or "").strip() not in {"UPGRADE", "确认升级"}:
        raise HTTPException(status_code=400, detail="请输入 UPGRADE 确认升级")
    mode = _normalize_mode(req.mode or cfg["mode"])
    _validate_image(cfg["image"])

    with _UPGRADE_TASK_LOCK:
        if _has_running_upgrade():
            raise HTTPException(status_code=409, detail="已有升级任务正在运行")
        run_id = f"upgrade_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        update_task_progress(run_id, "系统升级", 0, "running")
        _set_detail(run_id, mode=mode, image=cfg["image"])
        threading.Thread(target=_upgrade_worker, args=(run_id, mode, cfg), daemon=True).start()
    return {"status": "ok", "run_id": run_id, "message": "升级任务已启动"}
