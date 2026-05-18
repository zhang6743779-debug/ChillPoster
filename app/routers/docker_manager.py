import os
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.docker_api import DockerAPI, DockerApiError, build_replacement_container_payload
from core.logger import logger


router = APIRouter(prefix="/api/docker", tags=["DockerManager"])


class ContainerActionPayload(BaseModel):
    action: Literal["start", "stop", "restart", "remove", "update"] = "restart"
    force: bool = False
    image: str = ""


class PullImagePayload(BaseModel):
    image: str


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


def _recreate_container(api: DockerAPI, container_id: str, image: str) -> dict:
    image = _require_image_ref(image)
    info = api.inspect_container(container_id)
    old_name = str(info.get("Name") or "").strip("/")
    if not old_name:
        raise RuntimeError("无法读取容器名称")
    was_running = bool((info.get("State") or {}).get("Running"))
    old_id = str(info.get("Id") or container_id)
    backup_name = f"{old_name}-backup-{int(time.time())}"
    payload = build_replacement_container_payload(info, image)

    api.pull_image(image)
    if was_running:
        api.stop_container(old_id, timeout=20)
    api.rename_container(old_id, backup_name)
    new_id = ""
    try:
        created = api.create_container(old_name, payload)
        new_id = str((created or {}).get("Id") or "")
        if not new_id:
            raise RuntimeError(f"新容器创建失败: {created}")
        if was_running:
            api.start_container(new_id)
        api.delete_container(old_id, force=True)
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
        for row in rows or []:
            stats = None
            if row.get("State") == "running":
                try:
                    stats = api.container_stats(row.get("Id"))
                except Exception:
                    stats = None
            containers.append(_normalize_container(row, stats))
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
            result = _recreate_container(api, container_id, payload.image)
            return {"status": "ok", "message": "容器已更新并重建", "container": result}
        raise HTTPException(status_code=400, detail="不支持的操作")
    except Exception as e:
        _api_error(e)


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


@router.post("/images/pull")
def pull_image(payload: PullImagePayload):
    try:
        image = _require_image_ref(payload.image)
        _docker().pull_image(image)
        return {"status": "ok", "message": f"镜像已拉取: {image}"}
    except Exception as e:
        _api_error(e)


@router.delete("/images/{image_id:path}")
def delete_image(image_id: str, force: bool = False):
    try:
        _docker().delete_image(image_id, force=force)
        return {"status": "ok", "message": "镜像已删除"}
    except Exception as e:
        _api_error(e)
