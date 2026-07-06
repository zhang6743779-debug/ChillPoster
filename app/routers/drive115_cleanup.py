import json
import os
import time
import uuid
from typing import List

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.media_organize_115_ops import _get_115_client
from app.services.task_service import task_service_instance
from app.services.cloud_drive_provider import get_cloud_drive, is_drive_115
from core.logger import logger


router = APIRouter(prefix="/api/drive115_cleanup", tags=["Drive115Cleanup"])
CONFIG_FILE = "config/drive115_cleanup_tasks.json"


class CleanupFolder(BaseModel):
    cid: str
    name: str = ""
    path: str = ""


class CleanupTaskPayload(BaseModel):
    name: str
    cron: str
    enabled: bool = True
    drive_index: int = 0
    clear_recycle_bin: bool = True
    folders: List[CleanupFolder]


class Browse115Payload(BaseModel):
    cid: str = "0"
    drive_index: int = 0


class TogglePayload(BaseModel):
    enabled: bool


def _ensure_config_dir():
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)


def _load_tasks() -> list[dict]:
    if not os.path.exists(CONFIG_FILE):
        return []
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_tasks(tasks: list[dict]):
    _ensure_config_dir()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def _validate_cron(cron: str) -> str:
    value = str(cron or "").strip()
    try:
        CronTrigger.from_crontab(value)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cron 表达式无效: {e}")
    return value


def _normalize_folder(folder: CleanupFolder) -> dict:
    cid = str(folder.cid or "").strip()
    name = str(folder.name or "").strip()
    path = str(folder.path or "").strip()
    if cid == "0" or path in {"", "/", "根目录"}:
        raise HTTPException(status_code=400, detail="禁止选择根目录")
    return {"cid": cid, "name": name or path or cid, "path": path or name or cid}


def _normalize_payload(payload: CleanupTaskPayload, existing: dict | None = None) -> dict:
    name = str(payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="任务名称不能为空")
    cron = _validate_cron(payload.cron)

    folders = []
    seen = set()
    for folder in payload.folders or []:
        normalized = _normalize_folder(folder)
        if normalized["cid"] in seen:
            continue
        seen.add(normalized["cid"])
        folders.append(normalized)
    if not folders:
        raise HTTPException(status_code=400, detail="请至少选择一个云盘文件夹")

    base = dict(existing or {})
    base.update({
        "name": name,
        "cron": cron,
        "enabled": bool(payload.enabled),
        "drive_index": int(payload.drive_index or 0),
        "clear_recycle_bin": bool(payload.clear_recycle_bin),
        "folders": folders,
    })
    base.setdefault("last_run_at", None)
    base.setdefault("last_status", None)
    base.setdefault("last_message", None)
    base.setdefault("last_deleted_count", 0)
    return base


def _find_task(tasks: list[dict], task_id: str) -> tuple[int, dict]:
    for idx, task in enumerate(tasks):
        if str(task.get("id") or "") == str(task_id):
            return idx, task
    raise HTTPException(status_code=404, detail="任务不存在")


def _refresh_jobs():
    try:
        task_service_instance.refresh_selected_cleanup_jobs()
    except Exception as e:
        logger.warning(f"[CleanUp] 刷新云盘定时清空任务失败: {e}")


@router.get("/tasks")
def get_tasks():
    return {"tasks": _load_tasks()}


@router.post("/tasks")
def create_task(payload: CleanupTaskPayload):
    tasks = _load_tasks()
    task = _normalize_payload(payload)
    task["id"] = f"drive115_cleanup_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    tasks.append(task)
    _save_tasks(tasks)
    _refresh_jobs()
    return {"status": "ok", "task": task}


@router.post("/tasks/{task_id}")
def update_task(task_id: str, payload: CleanupTaskPayload):
    tasks = _load_tasks()
    idx, existing = _find_task(tasks, task_id)
    updated = _normalize_payload(payload, existing=existing)
    updated["id"] = task_id
    tasks[idx] = updated
    _save_tasks(tasks)
    _refresh_jobs()
    return {"status": "ok", "task": updated}


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    tasks = _load_tasks()
    idx, _ = _find_task(tasks, task_id)
    tasks.pop(idx)
    _save_tasks(tasks)
    _refresh_jobs()
    return {"status": "ok"}


@router.post("/tasks/{task_id}/toggle")
def toggle_task(task_id: str, payload: TogglePayload):
    tasks = _load_tasks()
    idx, task = _find_task(tasks, task_id)
    task["enabled"] = bool(payload.enabled)
    tasks[idx] = task
    _save_tasks(tasks)
    _refresh_jobs()
    return {"status": "ok", "task": task}


@router.post("/tasks/{task_id}/run")
def run_task(task_id: str):
    tasks = _load_tasks()
    _, task = _find_task(tasks, task_id)
    _normalize_payload(CleanupTaskPayload(**{
        "name": task.get("name", ""),
        "cron": task.get("cron", ""),
        "enabled": task.get("enabled", True),
        "drive_index": task.get("drive_index", 0),
        "clear_recycle_bin": task.get("clear_recycle_bin", True),
        "folders": task.get("folders", []),
    }), existing=task)
    result = task_service_instance.run_selected_cleanup_task(task, manual=True)
    return {"status": result.get("status", "ok"), "result": result}


@router.post("/browse115")
def browse_115(payload: Browse115Payload):
    try:
        if not is_drive_115(int(payload.drive_index or 0)):
            cloud = get_cloud_drive(int(payload.drive_index or 0))
            return cloud.list(payload.cid, include_files=False)

        client = _get_115_client(int(payload.drive_index or 0))
        cid = str(payload.cid or "0").strip() or "0"
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
                dirs.append({"name": item.get("fn", ""), "cid": str(item.get("fid", ""))})
        return {"status": "ok", "dirs": dirs}
    except Exception as e:
        return {"status": "error", "message": f"浏览失败: {e}", "dirs": []}
