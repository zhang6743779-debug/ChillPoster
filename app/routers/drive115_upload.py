import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.drive115_upload_service import drive115_upload_service


router = APIRouter(prefix="/api/drive115_upload", tags=["Drive115Upload"])


class UploadTaskPayload(BaseModel):
    name: str
    enabled: bool = True
    drive_index: int = 0
    local_folder: str
    target_cid: str
    target_name: str = ""
    target_path: str = ""
    watch_mode: str = "realtime"
    include_existing_on_start: bool = False
    delete_local_after_success: bool = False
    concurrency: int = 1


class TogglePayload(BaseModel):
    enabled: bool


class Browse115Payload(BaseModel):
    cid: str = "0"
    drive_index: int = 0


class LocalBrowsePayload(BaseModel):
    path: str = "/"


class RetryPayload(BaseModel):
    job_id: str


class ScanPayload(BaseModel):
    force: bool = True


def _payload_dict(payload: BaseModel) -> dict[str, Any]:
    return payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()


def _raise_for_error(exc: Exception):
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc).strip("'"))
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(status_code=500, detail=str(exc))


@router.get("/tasks")
def get_tasks():
    return {"tasks": drive115_upload_service.list_tasks()}


@router.post("/tasks")
def create_task(payload: UploadTaskPayload):
    try:
        task = drive115_upload_service.create_task(_payload_dict(payload))
        return {"status": "ok", "task": task}
    except Exception as e:
        _raise_for_error(e)


@router.post("/tasks/{task_id}")
def update_task(task_id: str, payload: UploadTaskPayload):
    try:
        task = drive115_upload_service.update_task(task_id, _payload_dict(payload))
        return {"status": "ok", "task": task}
    except Exception as e:
        _raise_for_error(e)


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    try:
        drive115_upload_service.delete_task(task_id)
        return {"status": "ok"}
    except Exception as e:
        _raise_for_error(e)


@router.post("/tasks/{task_id}/toggle")
def toggle_task(task_id: str, payload: TogglePayload):
    try:
        task = drive115_upload_service.toggle_task(task_id, payload.enabled)
        return {"status": "ok", "task": task}
    except Exception as e:
        _raise_for_error(e)


@router.post("/tasks/{task_id}/scan")
def scan_task(task_id: str, payload: ScanPayload | None = None):
    try:
        return drive115_upload_service.scan_task(task_id, force=True if payload is None else payload.force)
    except Exception as e:
        _raise_for_error(e)


@router.get("/status")
def get_status():
    return drive115_upload_service.get_status()


@router.get("/tasks/{task_id}/status")
def get_task_status(task_id: str):
    try:
        return drive115_upload_service.get_task_status(task_id)
    except Exception as e:
        _raise_for_error(e)


@router.post("/tasks/{task_id}/retry")
def retry_file(task_id: str, payload: RetryPayload):
    try:
        return drive115_upload_service.retry_file(task_id, payload.job_id)
    except Exception as e:
        _raise_for_error(e)


@router.post("/tasks/{task_id}/clear_history")
def clear_history(task_id: str):
    try:
        return drive115_upload_service.clear_history(task_id)
    except Exception as e:
        _raise_for_error(e)


@router.post("/browse115")
def browse_115(payload: Browse115Payload):
    try:
        return drive115_upload_service.browse_115(payload.cid, payload.drive_index)
    except Exception as e:
        return {"status": "error", "message": f"浏览失败: {e}", "dirs": []}


@router.post("/browse_local")
def browse_local(payload: LocalBrowsePayload):
    try:
        target = os.path.abspath(os.path.expanduser(payload.path or "/"))
        if not os.path.isdir(target):
            return {"status": "error", "message": "目录不存在", "dirs": []}
        dirs = []
        parent = os.path.dirname(target.rstrip(os.sep))
        if parent and parent != target:
            dirs.append({"name": "..", "path": parent})
        for entry in sorted(os.listdir(target)):
            full = os.path.join(target, entry)
            if os.path.isdir(full) and not entry.startswith("."):
                dirs.append({"name": entry, "path": full})
        return {"status": "ok", "dirs": dirs, "current": target}
    except PermissionError:
        return {"status": "error", "message": "无权限访问", "dirs": []}
    except Exception as e:
        return {"status": "error", "message": f"浏览失败: {e}", "dirs": []}
