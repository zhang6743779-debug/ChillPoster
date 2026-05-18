import os
import threading
import time
from typing import Literal
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.docker_api import DockerAPI, DockerApiError, build_replacement_container_payload, get_current_container_id
from core.configs import global_config
from core.logger import logger


router = APIRouter(prefix="/api/docker", tags=["DockerManager"])
_UPDATE_TASKS: dict[str, dict] = {}
_UPDATE_TASK_LOCK = threading.Lock()


class ContainerActionPayload(BaseModel):
    action: Literal["start", "stop", "restart", "remove", "update"] = "restart"
    force: bool = False
    image: str = ""


class PullImagePayload(BaseModel):
    image: str


class CheckUpdatesPayload(BaseModel):
    images: list[str] = []


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
        registry = "registry-1.docker.io"
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
        resp = client.get(url, headers=headers)
    return resp.status_code, resp.headers.get("Docker-Content-Digest", ""), resp.headers.get("WWW-Authenticate", "")


def _parse_auth_params(header: str) -> dict:
    if not header.lower().startswith("bearer "):
        return {}
    result = {}
    for part in header[7:].split(","):
        key, _, value = part.strip().partition("=")
        if key and value:
            result[key] = value.strip().strip('"')
    return result


def _remote_manifest_digest(image: str) -> str:
    registry, repo, tag = _parse_image_ref(image)
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
    if status >= 400:
        raise RuntimeError(f"远程镜像查询失败: HTTP {status}")
    if not digest:
        raise RuntimeError("远程镜像未返回 digest")
    return digest


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
            pull_result = api.pull_image(image)
            if isinstance(pull_result, list):
                for item in pull_result[-12:]:
                    if not isinstance(item, dict):
                        continue
                    status = item.get("status") or item.get("stream") or ""
                    detail = item.get("id") or ""
                    if status:
                        _task_log(run_id, f"{detail + ': ' if detail else ''}{status}".strip())
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

        max_workers = min(8, max(2, len(images)))
        logger.info(
            f"[DockerManager] 开始检查 {len(images)} 个镜像更新"
            f"{'（使用代理）' if str(global_config.proxy_url or '').strip() else '（直连）'}"
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(_check_image_update, api, image): image for image in images}
            for future in as_completed(future_map):
                image = future_map[future]
                try:
                    result[image] = future.result()
                except Exception as e:
                    result[image] = {
                        "image": image,
                        "update_available": False,
                        "message": str(e),
                    }
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
