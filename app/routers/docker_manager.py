import json
import os
import threading
import time
from typing import Literal
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.docker_api import DockerAPI, DockerApiError, build_replacement_container_payload, get_current_container_id
from core.configs import global_config
from core.logger import logger


router = APIRouter(prefix="/api/docker", tags=["DockerManager"])
_UPDATE_TASKS: dict[str, dict] = {}
_UPDATE_TASK_LOCK = threading.Lock()
_AUTO_UPDATE_CONFIG_PATH = os.path.join("config", "docker_auto_update.json")
_AUTO_UPDATE_CONFIG_LOCK = threading.Lock()
_AUTO_UPDATE_RUN_LOCK = threading.Lock()
_AUTO_UPDATE_JOB_ID = "docker_auto_update"
_AUTO_UPDATE_INTERVAL_MINUTES = 30
_SCHEDULED_RESTART_CONFIG_PATH = os.path.join("config", "docker_scheduled_restart.json")
_SCHEDULED_RESTART_CONFIG_LOCK = threading.Lock()
_SCHEDULED_RESTART_JOB_PREFIX = "docker_scheduled_restart_"
_MEMORY_RESTART_JOB_ID = "docker_memory_auto_restart"
_MEMORY_RESTART_INTERVAL_SECONDS = 60
_MEMORY_RESTART_COOLDOWN_SECONDS = 30 * 60
_MEMORY_RESTART_RUN_LOCK = threading.Lock()
_DOCKER_SCHEDULER = None
_DOCKER_HUB_REGISTRY = "registry-1.docker.io"
_DOCKER_HUB_CHALLENGE_HOST = "index.docker.io"
_DOCKER_HUB_ACCELERATOR_HOSTS = [
    "docker.1ms.run",
    "docker.m.daocloud.io",
    "docker.1panel.top",
    "docker.1panel.live",
    "proxy.1panel.live",
    "dockerproxy.1panel.live",
    "docker.1panel.dev",
    "docker.anye.in",
    "hub.rat.dev",
    "docker.amingg.com",
]
_DOCKER_HUB_HOST_CACHE = {"ts": 0.0, "hosts": []}
_DOCKER_HUB_HOST_CACHE_TTL = 1800


class RegistryRateLimitError(RuntimeError):
    pass


class ContainerActionPayload(BaseModel):
    action: Literal["start", "stop", "restart", "remove", "update"] = "restart"
    force: bool = False
    image: str = ""


class PullImagePayload(BaseModel):
    image: str


class CheckUpdatesPayload(BaseModel):
    images: list[str] = []


class AutoUpdatePayload(BaseModel):
    enabled: bool = False
    image: str = ""


class ScheduledRestartPayload(BaseModel):
    enabled: bool = False
    mode: Literal["time", "memory"] = "time"
    time: str = ""
    memory_limit_mb: float = 0


def _docker() -> DockerAPI:
    if not os.path.exists("/var/run/docker.sock"):
        raise HTTPException(status_code=503, detail="未挂载 /var/run/docker.sock，无法管理 Docker")
    return DockerAPI(timeout=45)


def _api_error(e: Exception):
    if isinstance(e, HTTPException):
        raise e
    if isinstance(e, DockerApiError):
        raise HTTPException(status_code=e.status if e.status < 600 else 500, detail=e.message)
    raise HTTPException(status_code=500, detail=str(e))


def _decode_docker_stream(raw: bytes) -> str:
    if not raw:
        return ""
    if len(raw) < 8:
        return raw.decode("utf-8", errors="replace")
    chunks = []
    idx = 0
    try:
        while idx + 8 <= len(raw):
            size = int.from_bytes(raw[idx + 4:idx + 8], "big")
            idx += 8
            if size < 0 or idx + size > len(raw):
                return raw.decode("utf-8", errors="replace")
            chunks.append(raw[idx:idx + size])
            idx += size
        if idx == len(raw) and chunks:
            return b"".join(chunks).decode("utf-8", errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _calc_cpu_percent(stats: dict) -> float:
    cpu = stats.get("cpu_stats") or {}
    precpu = stats.get("precpu_stats") or {}
    cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (precpu.get("cpu_usage") or {}).get("total_usage", 0)
    system_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
    online_cpus = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    if cpu_delta > 0 and system_delta > 0:
        return round((cpu_delta / system_delta) * online_cpus * 100, 2)
    return 0.0


def _calc_memory(stats: dict) -> dict:
    memory = stats.get("memory_stats") or {}
    usage = int(memory.get("usage") or 0)
    limit = int(memory.get("limit") or 0)
    cache = int((memory.get("stats") or {}).get("cache") or 0)
    effective = max(0, usage - cache)
    return {
        "usage": effective,
        "limit": limit,
        "percent": round((effective / limit) * 100, 2) if limit else 0,
    }


def _ports_summary(ports: list[dict] | None) -> str:
    result = []
    for port in ports or []:
        private = port.get("PrivatePort")
        public = port.get("PublicPort")
        typ = port.get("Type") or "tcp"
        if public:
            result.append(f"{public}:{private}/{typ}")
        elif private:
            result.append(f"{private}/{typ}")
    return ", ".join(result)


def _normalize_container(row: dict, stats: dict | None = None) -> dict:
    names = [str(name).strip("/") for name in row.get("Names") or []]
    state = str(row.get("State") or "")
    status = str(row.get("Status") or "")
    item = {
        "id": str(row.get("Id") or ""),
        "short_id": str(row.get("Id") or "")[:12],
        "name": names[0] if names else str(row.get("Id") or "")[:12],
        "names": names,
        "image": str(row.get("Image") or ""),
        "image_id": str(row.get("ImageID") or ""),
        "state": state,
        "status": status,
        "created": int(row.get("Created") or 0),
        "ports": row.get("Ports") or [],
        "ports_text": _ports_summary(row.get("Ports") or []),
        "labels": row.get("Labels") or {},
        "cpu_percent": 0,
        "memory_usage": 0,
        "memory_limit": 0,
        "memory_percent": 0,
    }
    if stats:
        memory = _calc_memory(stats)
        item.update({
            "cpu_percent": _calc_cpu_percent(stats),
            "memory_usage": memory["usage"],
            "memory_limit": memory["limit"],
            "memory_percent": memory["percent"],
        })
    return item


def _normalize_container_name(value: str) -> str:
    return str(value or "").strip().strip("/")


def _load_auto_update_config() -> dict:
    with _AUTO_UPDATE_CONFIG_LOCK:
        try:
            if not os.path.exists(_AUTO_UPDATE_CONFIG_PATH):
                return {"containers": {}}
            with open(_AUTO_UPDATE_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"containers": {}}
            containers = data.get("containers")
            if not isinstance(containers, dict):
                data["containers"] = {}
            return data
        except Exception as e:
            logger.warning(f"[DockerManager] 自动更新配置读取失败: {e}")
            return {"containers": {}}


def _save_auto_update_config(data: dict):
    with _AUTO_UPDATE_CONFIG_LOCK:
        os.makedirs(os.path.dirname(_AUTO_UPDATE_CONFIG_PATH), exist_ok=True)
        tmp_path = f"{_AUTO_UPDATE_CONFIG_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _AUTO_UPDATE_CONFIG_PATH)


def _auto_update_enabled_names() -> set[str]:
    config = _load_auto_update_config()
    enabled = set()
    for name, item in (config.get("containers") or {}).items():
        if isinstance(item, dict) and item.get("enabled"):
            enabled.add(_normalize_container_name(name))
    return enabled


def _auto_update_setting_for(name: str) -> dict:
    config = _load_auto_update_config()
    item = (config.get("containers") or {}).get(_normalize_container_name(name)) or {}
    return item if isinstance(item, dict) else {}


def _save_auto_update_setting(name: str, setting: dict) -> dict:
    config = _load_auto_update_config()
    containers = config.setdefault("containers", {})
    clean_name = _normalize_container_name(name)
    if not clean_name:
        raise HTTPException(status_code=400, detail="无法识别容器名称")
    item = containers.get(clean_name) if isinstance(containers.get(clean_name), dict) else {}
    item.update(setting)
    item["container_name"] = clean_name
    item["updated_at"] = time.time()
    containers[clean_name] = item
    _save_auto_update_config(config)
    return item


def _mark_auto_update_check(name: str, **kwargs):
    try:
        _save_auto_update_setting(name, kwargs)
    except Exception as e:
        logger.warning(f"[DockerManager] 自动更新状态写入失败: {name} - {e}")


def _load_scheduled_restart_config() -> dict:
    with _SCHEDULED_RESTART_CONFIG_LOCK:
        try:
            if not os.path.exists(_SCHEDULED_RESTART_CONFIG_PATH):
                return {"containers": {}}
            with open(_SCHEDULED_RESTART_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"containers": {}}
            containers = data.get("containers")
            if not isinstance(containers, dict):
                data["containers"] = {}
            return data
        except Exception as e:
            logger.warning(f"[DockerManager] 定时重启配置读取失败: {e}")
            return {"containers": {}}


def _save_scheduled_restart_config(data: dict):
    with _SCHEDULED_RESTART_CONFIG_LOCK:
        os.makedirs(os.path.dirname(_SCHEDULED_RESTART_CONFIG_PATH), exist_ok=True)
        tmp_path = f"{_SCHEDULED_RESTART_CONFIG_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _SCHEDULED_RESTART_CONFIG_PATH)


def _scheduled_restart_setting_for(name: str) -> dict:
    config = _load_scheduled_restart_config()
    item = (config.get("containers") or {}).get(_normalize_container_name(name)) or {}
    return item if isinstance(item, dict) else {}


def _restart_mode_for(setting: dict) -> str:
    mode = str((setting or {}).get("mode") or "time").strip().lower()
    return "memory" if mode == "memory" else "time"


def _validate_restart_time(value: str) -> tuple[int, int, str]:
    raw = str(value or "").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except Exception:
        raise HTTPException(status_code=400, detail="定时重启时间格式应为 HH:mm")
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise HTTPException(status_code=400, detail="定时重启时间必须在 00:00 到 23:59 之间")
    return hour, minute, f"{hour:02d}:{minute:02d}"


def _validate_memory_limit_mb(value) -> int:
    try:
        limit = int(round(float(value or 0)))
    except Exception:
        raise HTTPException(status_code=400, detail="内存重启阈值必须是数字")
    if limit <= 0:
        raise HTTPException(status_code=400, detail="内存重启阈值必须大于 0 MB")
    if limit > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="内存重启阈值不能超过 10485760 MB")
    return limit


def _save_scheduled_restart_setting(name: str, setting: dict) -> dict:
    config = _load_scheduled_restart_config()
    containers = config.setdefault("containers", {})
    clean_name = _normalize_container_name(name)
    if not clean_name:
        raise HTTPException(status_code=400, detail="无法识别容器名称")
    item = containers.get(clean_name) if isinstance(containers.get(clean_name), dict) else {}
    item.update(setting)
    item["container_name"] = clean_name
    item["updated_at"] = time.time()
    containers[clean_name] = item
    _save_scheduled_restart_config(config)
    return item


def _scheduled_restart_job_id(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in _normalize_container_name(name))
    return f"{_SCHEDULED_RESTART_JOB_PREFIX}{safe}"


def _normalize_image(row: dict) -> dict:
    tags = row.get("RepoTags") or []
    return {
        "id": str(row.get("Id") or ""),
        "short_id": str(row.get("Id") or "").replace("sha256:", "")[:12],
        "tags": tags,
        "name": tags[0] if tags else "<none>:<none>",
        "created": int(row.get("Created") or 0),
        "size": int(row.get("Size") or 0),
        "shared_size": int(row.get("SharedSize") or 0),
        "containers": int(row.get("Containers") or 0),
    }


def _require_image_ref(image: str) -> str:
    value = str(image or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="镜像名称不能为空")
    if any(ch in value for ch in " ;|&`$<>\\"):
        raise HTTPException(status_code=400, detail="镜像名称包含非法字符")
    return value


def _parse_image_ref(image: str) -> tuple[str, str, str]:
    value = _require_image_ref(image)
    if "@" in value:
        value = value.split("@", 1)[0]
    last_slash = value.rfind("/")
    last_colon = value.rfind(":")
    tag = "latest"
    if last_colon > last_slash:
        tag = value[last_colon + 1:] or "latest"
        value = value[:last_colon]
    first = value.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        registry, repo = value.split("/", 1)
    else:
        registry = _DOCKER_HUB_REGISTRY
        repo = value
        if "/" not in repo:
            repo = f"library/{repo}"
    return registry, repo, tag


def _registry_client_kwargs() -> dict:
    global_config.load()
    proxy_url = str(global_config.proxy_url or "").strip()
    kwargs = {"timeout": 12.0, "follow_redirects": True}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


def _registry_request_digest(registry: str, repo: str, tag: str, token: str = "") -> tuple[int, str, str]:
    url = f"https://{registry}/v2/{repo}/manifests/{urllib.parse.quote(tag, safe='')}"
    headers = {
        "Accept": ", ".join([
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
        ])
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(**_registry_client_kwargs()) as client:
        resp = client.head(url, headers=headers)
        if resp.status_code in (405, 501) or not resp.headers.get("Docker-Content-Digest"):
            resp = client.get(url, headers=headers)
    return resp.status_code, resp.headers.get("Docker-Content-Digest", ""), resp.headers.get("WWW-Authenticate", "")


def _registry_ping(host: str) -> bool:
    try:
        with httpx.Client(**_registry_client_kwargs()) as client:
            resp = client.get(f"https://{host}/v2/")
        return resp.status_code in (200, 401)
    except Exception as e:
        logger.debug(f"[DockerManager] Registry 可用性检测失败: {host} - {e}")
        return False


def _docker_hub_candidate_hosts() -> list[str]:
    now = time.time()
    cached_hosts = _DOCKER_HUB_HOST_CACHE.get("hosts") or []
    if cached_hosts and now - float(_DOCKER_HUB_HOST_CACHE.get("ts") or 0) < _DOCKER_HUB_HOST_CACHE_TTL:
        return list(cached_hosts)

    candidates = [_DOCKER_HUB_REGISTRY, _DOCKER_HUB_CHALLENGE_HOST, *_DOCKER_HUB_ACCELERATOR_HOSTS]
    available = []
    for host in candidates:
        if host in available:
            continue
        if _registry_ping(host):
            available.append(host)
    if not available:
        available = [_DOCKER_HUB_REGISTRY]
    _DOCKER_HUB_HOST_CACHE.update({"ts": now, "hosts": available})
    logger.info(f"[DockerManager] Docker Hub 可用 registry 候选: {', '.join(available)}")
    return list(available)


def _parse_auth_params(header: str) -> dict:
    if not header.lower().startswith("bearer "):
        return {}
    result = {}
    for part in header[7:].split(","):
        key, _, value = part.strip().partition("=")
        if key and value:
            result[key] = value.strip().strip('"')
    return result


def _remote_manifest_digest_from(registry: str, repo: str, tag: str) -> str:
    status, digest, auth = _registry_request_digest(registry, repo, tag)
    if status == 401 and auth:
        params = _parse_auth_params(auth)
        realm = params.get("realm")
        if realm:
            query = {
                "service": params.get("service", registry),
                "scope": params.get("scope", f"repository:{repo}:pull"),
            }
            with httpx.Client(**_registry_client_kwargs()) as client:
                token_resp = client.get(realm, params=query)
                token_resp.raise_for_status()
                token = token_resp.json().get("token") or token_resp.json().get("access_token") or ""
            if token:
                status, digest, _ = _registry_request_digest(registry, repo, tag, token)
    if status == 429:
        raise RegistryRateLimitError("远程镜像查询失败: HTTP 429，Docker Hub 请求过多，请稍后再试")
    if status >= 400:
        raise RuntimeError(f"远程镜像查询失败: HTTP {status}")
    if not digest:
        raise RuntimeError("远程镜像未返回 digest")
    return digest


def _remote_manifest_digest(image: str) -> str:
    registry, repo, tag = _parse_image_ref(image)
    hosts = _docker_hub_candidate_hosts() if registry == _DOCKER_HUB_REGISTRY else [registry]
    errors = []
    for host in hosts:
        try:
            return _remote_manifest_digest_from(host, repo, tag)
        except RegistryRateLimitError as e:
            errors.append(f"{host}: {e}")
            continue
        except Exception as e:
            errors.append(f"{host}: {e}")
            if registry != _DOCKER_HUB_REGISTRY:
                break
            continue
    if registry == _DOCKER_HUB_REGISTRY and errors:
        if all("HTTP 429" in item for item in errors):
            raise RegistryRateLimitError("Docker Hub 及可用镜像源均返回 HTTP 429，请稍后再试")
        raise RuntimeError("; ".join(errors))
    raise RuntimeError("; ".join(errors) or "远程镜像查询失败")


def _local_image_digests(api: DockerAPI, image: str) -> set[str]:
    info = api.inspect_image(image)
    digests = set()
    for item in info.get("RepoDigests") or []:
        if "@sha256:" in item:
            digests.add(item.split("@", 1)[1])
    image_id = str(info.get("Id") or "")
    if image_id.startswith("sha256:"):
        digests.add(image_id)
    return digests


def _check_image_update(api: DockerAPI, image: str) -> dict:
    image = _require_image_ref(image)
    local = _local_image_digests(api, image)
    remote = _remote_manifest_digest(image)
    return {
        "image": image,
        "remote_digest": remote,
        "local_digests": sorted(local),
        "update_available": bool(remote and remote not in local),
        "message": "",
    }


def _recreate_container(api: DockerAPI, container_id: str, image: str, skip_pull: bool = False, progress=None) -> dict:
    image = _require_image_ref(image)
    info = api.inspect_container(container_id)
    old_name = str(info.get("Name") or "").strip("/")
    if not old_name:
        raise RuntimeError("无法读取容器名称")
    was_running = bool((info.get("State") or {}).get("Running"))
    old_id = str(info.get("Id") or container_id)
    backup_name = f"{old_name}-backup-{int(time.time())}"
    payload = build_replacement_container_payload(info, image)

    if not skip_pull:
        if progress:
            progress(2, "pull", f"正在拉取最新镜像: {image}")
        api.pull_image(image)
    if progress:
        progress(3, "stop", "正在停止并备份旧容器")
    if was_running:
        api.stop_container(old_id, timeout=20)
    api.rename_container(old_id, backup_name)
    new_id = ""
    try:
        if progress:
            progress(4, "create", "正在按原配置创建新容器")
        created = api.create_container(old_name, payload)
        new_id = str((created or {}).get("Id") or "")
        if not new_id:
            raise RuntimeError(f"新容器创建失败: {created}")
        if progress:
            progress(5, "start", "正在启动新容器")
        if was_running:
            api.start_container(new_id)
        api.delete_container(old_id, force=True)
        if progress:
            progress(6, "done", "容器更新完成")
        return {"id": new_id, "short_id": new_id[:12], "name": old_name, "image": image}
    except Exception:
        logger.error("[DockerManager] 容器更新失败，尝试回滚原容器", exc_info=True)
        if new_id:
            try:
                api.delete_container(new_id, force=True)
            except Exception:
                pass
        try:
            api.rename_container(old_id, old_name)
            if was_running:
                api.start_container(old_id)
        except Exception:
            logger.error("[DockerManager] 容器更新回滚失败", exc_info=True)
        raise


def _start_self_update_helper(api: DockerAPI, container_id: str, image: str) -> str:
    image = _require_image_ref(image)
    if not image.startswith("chillne/chillposter"):
        raise RuntimeError("当前 ChillPoster 容器只能使用 chillne/chillposter 镜像更新")

    helper_name = f"chillposter-docker-update-{uuid.uuid4().hex[:8]}"
    helper_code = (
        "import sys, time; "
        "from app.services.self_upgrade_helper import replace_container; "
        "time.sleep(2); "
        "replace_container(sys.argv[1], sys.argv[2], skip_pull=True)"
    )
    payload = {
        "Image": image,
        "Cmd": ["python", "-c", helper_code, container_id, image],
        "Env": ["PYTHONUNBUFFERED=1"],
        "HostConfig": {
            "Binds": ["/var/run/docker.sock:/var/run/docker.sock"],
            "AutoRemove": True,
            "NetworkMode": "bridge",
        },
        "Labels": {
            "chillposter.docker_update.helper": "true",
            "chillposter.docker_update.target": container_id,
        },
    }
    created = api.create_container(helper_name, payload)
    helper_id = str((created or {}).get("Id") or "")
    if not helper_id:
        raise RuntimeError(f"更新助手创建失败: {created}")
    api.start_container(helper_id)
    return helper_id


def _task_log(run_id: str, message: str, level: str = "info"):
    with _UPDATE_TASK_LOCK:
        task = _UPDATE_TASKS.setdefault(run_id, {})
        logs = task.setdefault("logs", [])
        logs.append({
            "time": time.strftime("%H:%M:%S"),
            "message": str(message),
            "level": level,
        })
        if len(logs) > 200:
            del logs[:-200]


def _set_update_task(run_id: str, **kwargs):
    with _UPDATE_TASK_LOCK:
        task = _UPDATE_TASKS.setdefault(run_id, {})
        task.setdefault("run_id", run_id)
        task.update(kwargs)
        task["updated_at"] = time.time()


def _update_progress(run_id: str, step_no: int, step_key: str, message: str, level: str = "info"):
    total = 6
    _set_update_task(
        run_id,
        step=step_key,
        step_no=step_no,
        total_steps=total,
        percent=max(1, min(100, int(step_no / total * 100))),
        message=message,
    )
    _task_log(run_id, message, level)


def _format_pull_event(item: dict) -> str:
    status = str(item.get("status") or item.get("stream") or "").strip()
    detail = str(item.get("id") or "").strip()
    progress = str(item.get("progress") or "").strip()
    error = item.get("error") or (item.get("errorDetail") or {}).get("message")
    if error:
        status = str(error).strip()
    message = f"{detail}: {status}" if detail and status else status or detail
    if progress:
        message = f"{message} {progress}".strip()
    return message.strip()


def _run_update_task(run_id: str, container_id: str, image: str):
    api = DockerAPI(timeout=900)
    try:
        image = _require_image_ref(image)
        _set_update_task(run_id, status="running", percent=3, image=image)
        _task_log(run_id, f"开始更新容器 {container_id[:12]}")

        _update_progress(run_id, 1, "inspect", "正在读取容器配置")
        info = api.inspect_container(container_id)
        name = str(info.get("Name") or "").strip("/") or container_id[:12]
        _set_update_task(run_id, container_name=name)
        _task_log(run_id, f"容器: {name}, 镜像: {image}")

        _update_progress(run_id, 2, "pull", f"正在拉取最新镜像: {image}")
        try:
            last_pull_message = {"value": ""}

            def pull_progress(item: dict):
                if not isinstance(item, dict):
                    return
                message = _format_pull_event(item)
                if not message or message == last_pull_message["value"]:
                    return
                last_pull_message["value"] = message
                _task_log(run_id, message, "error" if item.get("error") else "info")

            api.pull_image(image, progress=pull_progress)
        except Exception as e:
            _task_log(run_id, str(e), "error")
            raise RuntimeError(f"拉取镜像失败: {e}")

        def progress(step_no: int, step_key: str, message: str):
            _update_progress(run_id, step_no, step_key, message)

        current_container_id = get_current_container_id(api)
        if current_container_id and current_container_id == str(info.get("Id") or container_id):
            _set_update_task(run_id, self_update=True)
            _update_progress(run_id, 4, "helper", "正在启动更新助手，当前服务会短暂断开")
            helper_id = _start_self_update_helper(api, current_container_id, image)
            _set_update_task(
                run_id,
                status="restarting",
                percent=90,
                helper_id=helper_id,
                message="更新助手已启动，等待服务重启",
            )
            _task_log(run_id, f"更新助手已启动: {helper_id[:12]}，服务即将重启")
            return

        result = _recreate_container(api, container_id, image, skip_pull=True, progress=progress)
        _set_update_task(run_id, status="finished", percent=100, result=result, message="容器更新完成")
        _task_log(run_id, f"新容器已启动: {result.get('short_id')}")
    except Exception as e:
        logger.error(f"[DockerManager] 更新容器任务失败: {e}", exc_info=True)
        _set_update_task(run_id, status="error", percent=max(1, _UPDATE_TASKS.get(run_id, {}).get("percent", 1)), message=str(e))
        _task_log(run_id, str(e), "error")


def _has_active_update_task(container_id: str = "", container_name: str = "") -> bool:
    now = time.time()
    with _UPDATE_TASK_LOCK:
        for task in _UPDATE_TASKS.values():
            status = task.get("status")
            if status not in ("running", "restarting"):
                continue
            if container_id and task.get("container_id") == container_id:
                return True
            if container_name and task.get("container_name") == container_name:
                return True
            created_at = float(task.get("created_at") or task.get("updated_at") or 0)
            if task.get("auto_update") and created_at and now - created_at < 2 * 60 * 60:
                return True
    return False


def _start_auto_update_task(container_id: str, container_name: str, image: str) -> str:
    run_id = f"docker_auto_update_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    _set_update_task(
        run_id,
        status="running",
        percent=0,
        step="queued",
        step_no=0,
        total_steps=6,
        container_id=container_id,
        container_name=container_name,
        image=image,
        auto_update=True,
        logs=[],
        created_at=time.time(),
        message="自动更新任务已创建",
    )
    threading.Thread(target=_run_update_task, args=(run_id, container_id, image), daemon=True).start()
    return run_id


def run_docker_auto_update_once():
    if not _AUTO_UPDATE_RUN_LOCK.acquire(blocking=False):
        return
    try:
        if not os.path.exists("/var/run/docker.sock"):
            return
        config = _load_auto_update_config()
        settings = config.get("containers") or {}
        enabled_settings = {
            _normalize_container_name(name): item
            for name, item in settings.items()
            if _normalize_container_name(name) and isinstance(item, dict) and item.get("enabled")
        }
        if not enabled_settings:
            return

        api = DockerAPI(timeout=120)
        rows = api.list_containers(True) or []
        containers_by_name = {}
        for row in rows:
            for raw_name in row.get("Names") or []:
                name = _normalize_container_name(raw_name)
                if name:
                    containers_by_name[name] = row

        for name, setting in enabled_settings.items():
            row = containers_by_name.get(name)
            if not row:
                _mark_auto_update_check(name, last_checked_at=time.time(), last_error="容器不存在或已改名")
                continue
            container_id = str(row.get("Id") or "")
            if _has_active_update_task(container_id=container_id, container_name=name):
                continue

            image = str(setting.get("image") or row.get("Image") or "").strip()
            if not image or image.startswith("sha256:"):
                _mark_auto_update_check(name, last_checked_at=time.time(), last_error="容器镜像无法用于自动更新")
                continue

            try:
                info = _check_image_update(api, image)
                _mark_auto_update_check(
                    name,
                    image=image,
                    last_checked_at=time.time(),
                    last_error=info.get("message") or "",
                    update_available=bool(info.get("update_available")),
                    remote_digest=info.get("remote_digest") or "",
                )
                if not info.get("update_available"):
                    continue
                run_id = _start_auto_update_task(container_id, name, image)
                _mark_auto_update_check(name, last_update_run_id=run_id, last_update_at=time.time())
                logger.info(f"[DockerManager] 自动更新已启动: {name} -> {image}, run_id={run_id}")
                break
            except Exception as e:
                _mark_auto_update_check(name, last_checked_at=time.time(), last_error=str(e), update_available=False)
                logger.warning(f"[DockerManager] 自动更新检查失败: {name} - {e}")
    finally:
        _AUTO_UPDATE_RUN_LOCK.release()


def schedule_auto_update_job(scheduler):
    global _DOCKER_SCHEDULER
    _DOCKER_SCHEDULER = scheduler
    try:
        if scheduler.get_job(_AUTO_UPDATE_JOB_ID):
            return
        scheduler.add_job(
            run_docker_auto_update_once,
            IntervalTrigger(minutes=_AUTO_UPDATE_INTERVAL_MINUTES),
            id=_AUTO_UPDATE_JOB_ID,
            name="Docker 自动更新检查",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(f"[DockerManager] 自动更新检查任务已加载，每 {_AUTO_UPDATE_INTERVAL_MINUTES} 分钟执行一次")
    except Exception as e:
        logger.warning(f"[DockerManager] 自动更新检查任务加载失败: {e}")


def _resolve_container_id_by_name(api: DockerAPI, name: str) -> str:
    target = _normalize_container_name(name)
    for row in api.list_containers(True) or []:
        names = {_normalize_container_name(item) for item in row.get("Names") or []}
        if target in names:
            return str(row.get("Id") or "")
    return ""


def run_scheduled_restart(container_name: str):
    name = _normalize_container_name(container_name)
    if not name:
        return
    setting = _scheduled_restart_setting_for(name)
    if not setting.get("enabled") or _restart_mode_for(setting) != "time":
        return
    try:
        api = DockerAPI(timeout=120)
        container_id = _resolve_container_id_by_name(api, name)
        if not container_id:
            _save_scheduled_restart_setting(name, {"last_error": "容器不存在或已改名", "last_run_at": time.time()})
            logger.warning(f"[DockerManager] 定时重启失败，容器不存在或已改名: {name}")
            return
        _save_scheduled_restart_setting(name, {"last_error": "", "last_run_at": time.time(), "container_id": container_id})
        api.restart_container(container_id)
        _save_scheduled_restart_setting(name, {"last_error": "", "last_run_at": time.time(), "container_id": container_id})
        logger.info(f"[DockerManager] 定时重启完成: {name}")
    except Exception as e:
        _save_scheduled_restart_setting(name, {"last_error": str(e), "last_run_at": time.time()})
        logger.warning(f"[DockerManager] 定时重启失败: {name} - {e}")


def _register_scheduled_restart_job(name: str, setting: dict, scheduler=None):
    target_scheduler = scheduler or _DOCKER_SCHEDULER
    if not target_scheduler:
        return
    job_id = _scheduled_restart_job_id(name)
    try:
        if target_scheduler.get_job(job_id):
            target_scheduler.remove_job(job_id)
    except Exception:
        pass
    if not setting.get("enabled") or _restart_mode_for(setting) != "time":
        return
    hour, minute, clean_time = _validate_restart_time(setting.get("time") or "")
    target_scheduler.add_job(
        run_scheduled_restart,
        CronTrigger(hour=hour, minute=minute),
        args=[_normalize_container_name(name)],
        id=job_id,
        name=f"Docker 定时重启: {_normalize_container_name(name)} {clean_time}",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def _register_memory_restart_job(scheduler=None):
    target_scheduler = scheduler or _DOCKER_SCHEDULER
    if not target_scheduler:
        return
    try:
        if target_scheduler.get_job(_MEMORY_RESTART_JOB_ID):
            return
        target_scheduler.add_job(
            run_memory_restart_check_once,
            IntervalTrigger(seconds=_MEMORY_RESTART_INTERVAL_SECONDS),
            id=_MEMORY_RESTART_JOB_ID,
            name="Docker 内存自动重启检查",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    except Exception as e:
        logger.warning(f"[DockerManager] 内存自动重启检查任务加载失败: {e}")


def run_memory_restart_check_once():
    if not _MEMORY_RESTART_RUN_LOCK.acquire(blocking=False):
        return
    try:
        if not os.path.exists("/var/run/docker.sock"):
            return
        config = _load_scheduled_restart_config()
        enabled_settings = {
            _normalize_container_name(name): item
            for name, item in (config.get("containers") or {}).items()
            if (
                _normalize_container_name(name)
                and isinstance(item, dict)
                and item.get("enabled")
                and _restart_mode_for(item) == "memory"
            )
        }
        if not enabled_settings:
            return

        api = DockerAPI(timeout=120)
        rows = api.list_containers(True) or []
        containers_by_name = {}
        for row in rows:
            for raw_name in row.get("Names") or []:
                clean_name = _normalize_container_name(raw_name)
                if clean_name:
                    containers_by_name[clean_name] = row

        now = time.time()
        for name, setting in enabled_settings.items():
            try:
                threshold_mb = _validate_memory_limit_mb(setting.get("memory_limit_mb") or 0)
                threshold_bytes = threshold_mb * 1024 * 1024
                last_run_at = float(setting.get("last_run_at") or 0)
                if last_run_at and now - last_run_at < _MEMORY_RESTART_COOLDOWN_SECONDS:
                    continue

                row = containers_by_name.get(name)
                if not row:
                    _save_scheduled_restart_setting(name, {"last_error": "容器不存在或已改名", "last_checked_at": now})
                    logger.warning(f"[DockerManager] 内存自动重启检查失败，容器不存在或已改名: {name}")
                    continue
                if row.get("State") != "running":
                    _save_scheduled_restart_setting(name, {"last_error": "容器未运行", "last_checked_at": now})
                    continue

                container_id = str(row.get("Id") or "")
                stats = api.container_stats(container_id)
                memory = _calc_memory(stats)
                usage = int(memory.get("usage") or 0)
                if usage < threshold_bytes:
                    if setting.get("last_error"):
                        _save_scheduled_restart_setting(name, {"last_error": "", "last_checked_at": now})
                    continue

                restart_state = {
                    "last_error": "",
                    "last_run_at": now,
                    "last_checked_at": now,
                    "last_memory_restart_at": now,
                    "last_memory_usage": usage,
                    "memory_limit_mb": threshold_mb,
                    "container_id": container_id,
                }
                _save_scheduled_restart_setting(name, restart_state)
                api.restart_container(container_id)
                logger.info(
                    f"[DockerManager] 内存自动重启完成: {name}, "
                    f"usage={usage}, threshold={threshold_bytes}"
                )
            except HTTPException as e:
                detail = str(e.detail)
                _save_scheduled_restart_setting(name, {"last_error": detail, "last_checked_at": now})
                logger.warning(f"[DockerManager] 内存自动重启配置无效: {name} - {detail}")
            except Exception as e:
                _save_scheduled_restart_setting(name, {"last_error": str(e), "last_checked_at": now})
                logger.warning(f"[DockerManager] 内存自动重启失败: {name} - {e}")
    finally:
        _MEMORY_RESTART_RUN_LOCK.release()


def schedule_scheduled_restart_jobs(scheduler):
    global _DOCKER_SCHEDULER
    _DOCKER_SCHEDULER = scheduler
    try:
        config = _load_scheduled_restart_config()
        count = 0
        memory_count = 0
        for name, setting in (config.get("containers") or {}).items():
            if not isinstance(setting, dict) or not setting.get("enabled"):
                continue
            try:
                if _restart_mode_for(setting) == "memory":
                    memory_count += 1
                else:
                    _register_scheduled_restart_job(name, setting, scheduler=scheduler)
                    count += 1
            except Exception as e:
                logger.warning(f"[DockerManager] 自动重启任务加载失败: {name} - {e}")
        _register_memory_restart_job(scheduler)
        logger.info(f"[DockerManager] 自动重启任务已加载: 定时 {count} 个，内存阈值 {memory_count} 个")
    except Exception as e:
        logger.warning(f"[DockerManager] 自动重启任务加载失败: {e}")


@router.get("/status")
def docker_status():
    available = os.path.exists("/var/run/docker.sock")
    result = {"available": available, "message": "" if available else "未挂载 /var/run/docker.sock"}
    if not available:
        return result
    try:
        api = _docker()
        version = api.version()
        df = api.system_df()
        result.update({
            "server_version": version.get("Version"),
            "api_version": version.get("ApiVersion"),
            "os": version.get("Os"),
            "arch": version.get("Arch"),
            "containers": len(df.get("Containers") or []),
            "images": len(df.get("Images") or []),
        })
        return result
    except Exception as e:
        return {"available": False, "message": str(e)}


@router.get("/containers")
def list_containers():
    try:
        api = _docker()
        rows = api.list_containers(True)
        containers = []
        auto_enabled = _auto_update_enabled_names()
        for row in rows or []:
            stats = None
            if row.get("State") == "running":
                try:
                    stats = api.container_stats(row.get("Id"))
                except Exception:
                    stats = None
            item = _normalize_container(row, stats)
            item["auto_update_enabled"] = item["name"] in auto_enabled
            auto_setting = _auto_update_setting_for(item["name"])
            item["auto_update_image"] = auto_setting.get("image") or item["image"]
            item["auto_update_last_error"] = auto_setting.get("last_error") or ""
            restart_setting = _scheduled_restart_setting_for(item["name"])
            restart_mode = _restart_mode_for(restart_setting)
            restart_enabled = bool(restart_setting.get("enabled"))
            try:
                memory_limit_mb = int(round(float(restart_setting.get("memory_limit_mb") or 0)))
            except Exception:
                memory_limit_mb = 0
            item["auto_restart_enabled"] = restart_enabled
            item["auto_restart_mode"] = restart_mode
            item["auto_restart_time"] = restart_setting.get("time") or ""
            item["auto_restart_memory_limit_mb"] = memory_limit_mb
            item["auto_restart_last_error"] = restart_setting.get("last_error") or ""
            item["scheduled_restart_enabled"] = bool(restart_enabled and restart_mode == "time")
            item["scheduled_restart_time"] = restart_setting.get("time") or ""
            item["scheduled_restart_last_error"] = restart_setting.get("last_error") or ""
            containers.append(item)
        return {"containers": containers}
    except Exception as e:
        _api_error(e)


@router.post("/containers/{container_id}/action")
def container_action(container_id: str, payload: ContainerActionPayload):
    try:
        api = _docker()
        action = payload.action
        if action == "start":
            api.start_container(container_id)
            return {"status": "ok", "message": "容器已启动"}
        if action == "stop":
            api.stop_container(container_id)
            return {"status": "ok", "message": "容器已停止"}
        if action == "restart":
            api.restart_container(container_id)
            return {"status": "ok", "message": "容器已重启"}
        if action == "remove":
            api.delete_container(container_id, force=payload.force)
            return {"status": "ok", "message": "容器已删除"}
        if action == "update":
            image = _require_image_ref(payload.image)
            run_id = f"docker_update_{int(time.time())}_{uuid.uuid4().hex[:8]}"
            _set_update_task(
                run_id,
                status="running",
                percent=0,
                step="queued",
                step_no=0,
                total_steps=6,
                container_id=container_id,
                image=image,
                logs=[],
                created_at=time.time(),
                message="更新任务已创建",
            )
            threading.Thread(target=_run_update_task, args=(run_id, container_id, image), daemon=True).start()
            return {"status": "ok", "message": "容器更新任务已启动", "run_id": run_id}
        raise HTTPException(status_code=400, detail="不支持的操作")
    except Exception as e:
        _api_error(e)


@router.get("/update_tasks/{run_id}")
def get_update_task(run_id: str):
    task = _UPDATE_TASKS.get(run_id)
    if not task:
        raise HTTPException(status_code=404, detail="更新任务不存在")
    return task


@router.post("/containers/{container_id}/auto_update")
def set_container_auto_update(container_id: str, payload: AutoUpdatePayload):
    try:
        api = _docker()
        info = api.inspect_container(container_id)
        name = _normalize_container_name(info.get("Name") or container_id[:12])
        image = _require_image_ref(payload.image or (info.get("Config") or {}).get("Image") or "")
        setting = _save_auto_update_setting(name, {
            "enabled": bool(payload.enabled),
            "image": image,
            "container_id": str(info.get("Id") or container_id),
            "last_error": "",
        })
        return {
            "status": "ok",
            "container_name": name,
            "enabled": bool(setting.get("enabled")),
            "image": setting.get("image") or image,
            "message": f"{'已开启' if setting.get('enabled') else '已关闭'}自动更新: {name}",
        }
    except Exception as e:
        _api_error(e)


def _set_container_restart_policy(container_id: str, payload: ScheduledRestartPayload):
    try:
        api = _docker()
        info = api.inspect_container(container_id)
        name = _normalize_container_name(info.get("Name") or container_id[:12])
        existing = _scheduled_restart_setting_for(name)
        mode = payload.mode if payload.enabled else _restart_mode_for(existing)
        if mode not in ("time", "memory"):
            mode = "time"

        clean_time = str(existing.get("time") or "04:00")
        try:
            memory_limit_mb = _validate_memory_limit_mb(existing.get("memory_limit_mb") or 1024)
        except HTTPException:
            memory_limit_mb = 1024
        if payload.enabled:
            if mode == "time":
                _, _, clean_time = _validate_restart_time(payload.time)
            else:
                _, _, clean_time = _validate_restart_time(payload.time or existing.get("time") or "04:00")
                memory_limit_mb = _validate_memory_limit_mb(payload.memory_limit_mb)
        else:
            try:
                _, _, clean_time = _validate_restart_time(payload.time or existing.get("time") or "04:00")
            except HTTPException:
                clean_time = "04:00"
            try:
                memory_limit_mb = _validate_memory_limit_mb(payload.memory_limit_mb or existing.get("memory_limit_mb") or 1024)
            except HTTPException:
                memory_limit_mb = 1024

        setting = _save_scheduled_restart_setting(name, {
            "enabled": bool(payload.enabled),
            "mode": mode,
            "time": clean_time,
            "memory_limit_mb": memory_limit_mb,
            "container_id": str(info.get("Id") or container_id),
            "last_error": "",
        })
        _register_scheduled_restart_job(name, setting)
        _register_memory_restart_job()
        message_mode = "定时" if _restart_mode_for(setting) == "time" else "内存阈值"
        return {
            "status": "ok",
            "container_name": name,
            "enabled": bool(setting.get("enabled")),
            "mode": _restart_mode_for(setting),
            "time": setting.get("time") or "",
            "memory_limit_mb": int(setting.get("memory_limit_mb") or 0),
            "message": f"{'已设置' if setting.get('enabled') else '已关闭'}{message_mode}自动重启: {name}"
        }
    except Exception as e:
        _api_error(e)


@router.post("/containers/{container_id}/auto_restart")
def set_container_auto_restart(container_id: str, payload: ScheduledRestartPayload):
    return _set_container_restart_policy(container_id, payload)


@router.post("/containers/{container_id}/scheduled_restart")
def set_container_scheduled_restart(container_id: str, payload: ScheduledRestartPayload):
    return _set_container_restart_policy(container_id, payload)


@router.get("/containers/{container_id}/logs")
def container_logs(container_id: str, tail: int = 200):
    try:
        api = _docker()
        raw = api.container_logs(container_id, tail=tail)
        return {"logs": _decode_docker_stream(raw)}
    except Exception as e:
        _api_error(e)


@router.get("/images")
def list_images():
    try:
        api = _docker()
        return {"images": [_normalize_image(row) for row in api.list_images() or []]}
    except Exception as e:
        _api_error(e)


@router.post("/containers/check_updates")
def check_container_updates(payload: CheckUpdatesPayload):
    try:
        api = _docker()
        result = {}
        images = [
            image for image in sorted(set(str(item or "").strip() for item in payload.images or []))
            if image and not image.startswith("sha256:")
        ]
        if not images:
            return {"images": result}

        logger.info(
            f"[DockerManager] 开始检查 {len(images)} 个镜像更新"
            f"{'（使用代理）' if str(global_config.proxy_url or '').strip() else '（直连）'}"
        )

        grouped: dict[str, list[str]] = {}
        for image in images:
            registry, _, _ = _parse_image_ref(image)
            grouped.setdefault(registry, []).append(image)

        def mark_failed(image: str, error: Exception | str):
            message = str(error)
            logger.warning(f"[DockerManager] 镜像更新检查失败: {image} - {message}")
            result[image] = {
                "image": image,
                "update_available": False,
                "message": message,
            }

        for registry, registry_images in grouped.items():
            # Docker Hub 匿名 manifest 查询很容易触发 429；串行并在限流后停止同 registry 后续请求。
            if registry == _DOCKER_HUB_REGISTRY:
                rate_limited = False
                for image in registry_images:
                    if rate_limited:
                        result[image] = {
                            "image": image,
                            "update_available": False,
                            "message": "Docker Hub 已限流，已跳过本轮剩余 Docker Hub 镜像检查，请稍后再试",
                        }
                        continue
                    try:
                        result[image] = _check_image_update(api, image)
                    except RegistryRateLimitError as e:
                        rate_limited = True
                        mark_failed(image, e)
                    except Exception as e:
                        mark_failed(image, e)
                continue

            max_workers = min(3, max(1, len(registry_images)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {executor.submit(_check_image_update, api, image): image for image in registry_images}
                for future in as_completed(future_map):
                    image = future_map[future]
                    try:
                        result[image] = future.result()
                    except Exception as e:
                        mark_failed(image, e)
        return {"images": result}
    except Exception as e:
        _api_error(e)


@router.post("/images/pull")
def pull_image(payload: PullImagePayload):
    try:
        image = _require_image_ref(payload.image)
        _docker().pull_image(image)
        return {"status": "ok", "message": f"镜像已拉取: {image}"}
    except Exception as e:
        _api_error(e)


@router.post("/images/prune_unused")
def prune_unused_images():
    try:
        result = _docker().prune_images(all_unused=True)
        return {
            "status": "ok",
            "deleted": result.get("ImagesDeleted") or [],
            "space_reclaimed": int(result.get("SpaceReclaimed") or 0),
            "message": "未使用镜像已清理",
        }
    except Exception as e:
        _api_error(e)


@router.post("/images/prune_untagged")
def prune_untagged_images():
    try:
        result = _docker().prune_images(dangling_only=True)
        return {
            "status": "ok",
            "deleted": result.get("ImagesDeleted") or [],
            "space_reclaimed": int(result.get("SpaceReclaimed") or 0),
            "message": "无 Tag 镜像已清理",
        }
    except Exception as e:
        _api_error(e)


@router.delete("/images/{image_id:path}")
def delete_image(image_id: str, force: bool = False):
    try:
        _docker().delete_image(image_id, force=force)
        return {"status": "ok", "message": "镜像已删除"}
    except Exception as e:
        _api_error(e)
