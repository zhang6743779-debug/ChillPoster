import os
import shutil
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from app.services.real_library_service import (
    RUNTIME_STATE_KEYS,
    get_task_by_id,
    load_config,
    load_tasks,
    real_library_service_instance,
    save_config,
    save_tasks,
    test_emby_connection,
    validate_paths,
)
from core.emby_client import EmbyClient
from core.logger import logger


router = APIRouter(prefix="/api/real_library", tags=["RealLibrary"])


class RealLibraryConfigPayload(BaseModel):
    enabled: bool = True
    emby_name: str = "独立真实库"
    emby_url: str = ""
    emby_key: str = ""
    emby_public_host: str = ""
    source_root: str = ""
    link_root: str = ""
    tmdb_key: str = ""
    proxy_url: str = ""


class RealLibraryTaskPayload(BaseModel):
    id: Optional[str] = None
    name: str
    rss_url: str
    cron: str = "0 */4 * * *"
    content_type: str = "movies"
    enabled: bool = True
    last_entries: List[str] = Field(default_factory=list)
    entry_tmdb_map: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
    last_sync_at: Optional[float] = None


class RealLibraryTaskUpdatePayload(RealLibraryTaskPayload):
    id: str


class TogglePayload(BaseModel):
    id: str
    enabled: bool


def _normalize_task(data: dict) -> dict:
    task = dict(data or {})
    task["name"] = str(task.get("name") or "").strip()
    task["rss_url"] = str(task.get("rss_url") or "").strip()
    task["cron"] = str(task.get("cron") or "0 */4 * * *").strip()
    task["content_type"] = str(task.get("content_type") or "movies").strip() or "movies"
    task["enabled"] = bool(task.get("enabled", True))
    task.setdefault("last_entries", [])
    task.setdefault("entry_tmdb_map", {})
    task.setdefault("last_sync_at", None)
    if not task["name"] or not task["rss_url"]:
        raise HTTPException(status_code=400, detail="请填写任务名称和 RSS 地址")
    return task


def _build_client_from_saved_config() -> EmbyClient | None:
    cfg = load_config()
    url = str(cfg.get("emby_url") or "").strip()
    key = str(cfg.get("emby_key") or "").strip()
    if not url or not key:
        return None
    return EmbyClient(url, key, str(cfg.get("emby_public_host") or "").strip() or None)


@router.get("/config")
def get_real_library_config():
    return load_config()


@router.post("/save_config")
def save_real_library_config(payload: RealLibraryConfigPayload):
    cfg = save_config(payload.model_dump())
    return {"status": "ok", "message": "独立真实库配置已保存", "config": cfg}


@router.post("/test_emby")
def test_real_library_emby(payload: RealLibraryConfigPayload):
    return test_emby_connection(payload.model_dump())


@router.post("/validate_paths")
def validate_real_library_paths(payload: RealLibraryConfigPayload):
    return validate_paths(payload.model_dump())


@router.get("/tasks")
def get_real_library_tasks():
    return load_tasks()


@router.post("/create_task")
def create_real_library_task(payload: RealLibraryTaskPayload):
    tasks = load_tasks()
    task = _normalize_task(payload.model_dump())
    task["id"] = f"real_library_{int(time.time())}"
    tasks.append(task)
    save_tasks(tasks)
    if task.get("enabled", True):
        real_library_service_instance.add_job(task)
    return {"status": "ok", "task": task}


@router.post("/update_task")
def update_real_library_task(payload: RealLibraryTaskUpdatePayload):
    tasks = load_tasks()
    req = _normalize_task(payload.model_dump())
    found = False
    for idx, task in enumerate(tasks):
        if task.get("id") == payload.id:
            merged = dict(req)
            for key in RUNTIME_STATE_KEYS:
                if key in task:
                    merged[key] = task[key]
            tasks[idx] = merged
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="任务不存在")

    save_tasks(tasks)
    real_library_service_instance.remove_job(payload.id)
    if req.get("enabled", True):
        real_library_service_instance.add_job(req)
    return {"status": "ok", "task": req}


@router.post("/run_now")
def run_real_library_now(payload: dict = Body(...)):
    task_id = str(payload.get("id") or "").strip()
    if not task_id:
        raise HTTPException(status_code=400, detail="缺少任务 ID")
    task = get_task_by_id(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    cfg = load_config()
    if not cfg.get("enabled", True):
        raise HTTPException(status_code=400, detail="独立真实库已停用，请先启用并保存配置")
    if not str(cfg.get("link_root") or "").strip():
        raise HTTPException(status_code=400, detail="请先填写并保存真实库输出路径")
    if not str(cfg.get("emby_url") or "").strip() or not str(cfg.get("emby_key") or "").strip():
        raise HTTPException(status_code=400, detail="请先填写并保存 Emby 地址和 API Key")

    try:
        run_id = real_library_service_instance.enqueue(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "triggered", "run_id": run_id}


@router.post("/toggle_task")
def toggle_real_library_task(payload: TogglePayload):
    tasks = load_tasks()
    target = None
    for task in tasks:
        if task.get("id") == payload.id:
            target = task
            break
    if not target:
        raise HTTPException(status_code=404, detail="任务不存在")

    target["enabled"] = payload.enabled
    save_tasks(tasks)
    if payload.enabled:
        real_library_service_instance.add_job(target)
    else:
        real_library_service_instance.remove_job(payload.id)
    return {"status": "ok", "enabled": payload.enabled}


@router.post("/delete_task")
def delete_real_library_task(payload: dict = Body(...)):
    task_id = str(payload.get("id") or "").strip()
    delete_files = bool(payload.get("delete_files", False))
    if not task_id:
        raise HTTPException(status_code=400, detail="缺少任务 ID")

    tasks = load_tasks()
    task_to_delete = None
    remaining = []
    for task in tasks:
        if task.get("id") == task_id:
            task_to_delete = task
        else:
            remaining.append(task)
    if not task_to_delete:
        raise HTTPException(status_code=404, detail="任务不存在")

    save_tasks(remaining)
    real_library_service_instance.remove_job(task_id)

    if delete_files:
        cfg = load_config()
        link_root = str(cfg.get("link_root") or "").strip()
        target_path = os.path.join(link_root, task_to_delete["name"]) if link_root else ""
        if target_path and os.path.exists(target_path):
            shutil.rmtree(target_path)

        client = _build_client_from_saved_config()
        try:
            if client:
                libs = client.get_libraries()
                target_lib = next((lib for lib in libs or [] if lib.get("name") == task_to_delete["name"]), None)
                if target_lib:
                    client.delete_library(target_lib["id"])
        except Exception as e:
            logger.warning(f"[RealLibrary] 删除 Emby 媒体库失败: {e}")
        finally:
            try:
                client.close()
            except Exception:
                pass

    return {"status": "ok"}
