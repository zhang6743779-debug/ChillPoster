from __future__ import annotations

import copy
import hashlib
import json
import os
import posixpath
import queue
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from p115client import P115Client
from p115client.tool.attr import get_attr, normalize_attr_simple
from p115client.tool.iterdir import iter_files_with_path
from p115pickcode import to_id

from app.services.media_organize_115_ops import _check_and_move, _ensure_115_dir_chain_cached, _mkdir_115_dir
from core.logger import logger


CONFIG_FILE = Path("config/drive115_upload_tasks.json")
STATE_FILE = Path("config/drive115_upload_state.json")
CONFIG_302_FILE = Path("config/config_302.json")
WATCH_SCAN_INTERVAL_SECONDS = 2
FILE_STABLE_SECONDS = 5
MAX_QUEUE_SIZE = 200
MAX_HISTORY = 100
DEFAULT_UPLOAD_WORKERS = 5
MAX_UPLOAD_WORKERS = 30
CLOUD_RAPID_LIST_LIMIT = 1150
CLOUD_RAPID_MAX_FILES = 0
CLOUD_RAPID_RESULT_LIMIT = 200
CLOUD_RAPID_JOB_KEEP_SECONDS = 24 * 60 * 60
CLOUD_RAPID_JOB_HISTORY_LIMIT = 20
CLOUD_RAPID_DEFAULT_CONCURRENCY = 4
CLOUD_RAPID_MAX_CONCURRENCY = 4
CLOUD_RAPID_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36"
CLOUD_RAPID_CACHE_DB = Path("config/drive115_cloud_rapid_cache.db")
CLOUD_RAPID_SERVER_DB_PREFIX = "drive115_cloud_server_items"
CLOUD_RAPID_URL_TTL_BUFFER_SECONDS = 180
CLOUD_RAPID_FALLBACK_URL_TTL_SECONDS = 600
CLOUD_RAPID_COLLECT_SKIP_SIZE = 1024 * 1024 * 115
CLOUD_RAPID_AUTH_COOLDOWN_SECONDS = 10
TEMP_SUFFIXES = (
    ".crdownload",
    ".part",
    ".tmp",
    ".!qb",
    ".!ut",
    ".download",
)


class CloudRapidCancelled(RuntimeError):
    pass


class Drive115UploadService:
    def __init__(self):
        self._lock = threading.RLock()
        self._loaded = False
        self._started = False
        self._tasks: list[dict[str, Any]] = []
        self._state: dict[str, Any] = {"tasks": {}}
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._watchers: dict[str, threading.Thread] = {}
        self._watcher_stops: dict[str, threading.Event] = {}
        self._workers: list[threading.Thread] = []
        self._client_cache: dict[str, P115Client] = {}
        self._stability: dict[str, tuple[int, int, float]] = {}
        self._seen_keys: set[str] = set()
        self._queued_keys: set[str] = set()
        self._active_keys: set[str] = set()
        self._completed_keys: set[str] = set()
        self._failed_keys: set[str] = set()
        self._queued_count: dict[str, int] = {}
        self._active_count: dict[str, int] = {}
        self._active_jobs: dict[str, dict[str, Any]] = {}
        self._dir_chain_cache: dict[tuple[str, str, str, str], str] = {}
        self._cloud_rapid_jobs: dict[str, dict[str, Any]] = {}
        self._cloud_rapid_cancel_events: dict[str, threading.Event] = {}
        self._cloud_range_semaphore = threading.Semaphore(2)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._load_locked()
            self._stop_event.clear()
            self._started = True
            self._ensure_workers_locked()
            self._sync_watchers_locked()
        logger.trace("[Drive115Upload] 本地监听上传服务已启动")

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
            self._stop_event.set()
            watcher_events = list(self._watcher_stops.values())
            watcher_threads = list(self._watchers.values())
            worker_threads = list(self._workers)
            self._watchers.clear()
            self._watcher_stops.clear()
        for event in watcher_events:
            event.set()
        for thread in watcher_threads:
            if thread.is_alive():
                thread.join(timeout=1.5)
        for thread in worker_threads:
            if thread.is_alive():
                thread.join(timeout=1.5)
        with self._lock:
            self._workers = [thread for thread in self._workers if thread.is_alive()]
        logger.info("[Drive115Upload] 本地监听上传服务已停止")

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load_locked()
            return copy.deepcopy(self._tasks)

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._load_locked()
            now = int(time.time())
            task = self._normalize_task(payload)
            task["id"] = f"drive115_upload_{now}_{uuid.uuid4().hex[:8]}"
            task["created_at"] = now
            task["updated_at"] = now
            self._tasks.append(task)
            self._ensure_task_state_locked(task["id"])
            self._save_tasks_locked()
            self._save_state_locked()
            if self._started:
                self._ensure_workers_locked()
                self._start_watcher_locked(task)
            return copy.deepcopy(task)

    def update_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._load_locked()
            idx, existing = self._find_task_locked(task_id)
            task = self._normalize_task(payload, existing=existing)
            task["id"] = task_id
            task["created_at"] = existing.get("created_at") or int(time.time())
            task["updated_at"] = int(time.time())
            self._tasks[idx] = task
            self._clear_task_runtime_keys_locked(task_id)
            self._ensure_task_state_locked(task_id)
            self._save_tasks_locked()
            if self._started:
                self._restart_watcher_locked(task_id, task)
                self._ensure_workers_locked()
            return copy.deepcopy(task)

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            self._load_locked()
            idx, _ = self._find_task_locked(task_id)
            self._stop_watcher_locked(task_id)
            self._tasks.pop(idx)
            self._state.setdefault("tasks", {}).pop(task_id, None)
            self._clear_task_runtime_keys_locked(task_id)
            self._queued_count.pop(task_id, None)
            self._active_count.pop(task_id, None)
            self._save_tasks_locked()
            self._save_state_locked()

    def toggle_task(self, task_id: str, enabled: bool) -> dict[str, Any]:
        with self._lock:
            self._load_locked()
            idx, task = self._find_task_locked(task_id)
            task = dict(task)
            task["enabled"] = bool(enabled)
            task["updated_at"] = int(time.time())
            self._tasks[idx] = task
            self._save_tasks_locked()
            if self._started:
                if task["enabled"]:
                    self._start_watcher_locked(task)
                else:
                    self._stop_watcher_locked(task_id)
            return copy.deepcopy(task)

    def scan_task(self, task_id: str, force: bool = True) -> dict[str, Any]:
        task = self._get_task_copy(task_id)
        if not task:
            raise KeyError("任务不存在")
        count = self._scan_task_files(task, force=force)
        return {"status": "ok", "queued": count}

    def retry_file(self, task_id: str, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._load_locked()
            task = self._get_task_locked(task_id)
            if not task:
                raise KeyError("任务不存在")
            state = self._ensure_task_state_locked(task_id)
            failed = state.get("failed", [])
            idx = next((i for i, item in enumerate(failed) if str(item.get("job_id")) == str(job_id)), -1)
            if idx < 0:
                raise KeyError("失败记录不存在")
            record = failed.pop(idx)
            old_key = str(record.get("key") or "")
            if old_key:
                self._failed_keys.discard(old_key)
            self._save_state_locked()
        path = str(record.get("path") or "")
        if not path or not os.path.isfile(path):
            raise FileNotFoundError("本地文件不存在，无法重试")
        stat = os.stat(path)
        queued = self._enqueue_file(
            task,
            path,
            stat.st_size,
            int(stat.st_mtime_ns),
            force=True,
            attempts=int(record.get("attempts") or 1) + 1,
            source="retry",
        )
        return {"status": "ok", "queued": 1 if queued else 0}

    def clear_history(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            self._load_locked()
            self._find_task_locked(task_id)
            state = self._ensure_task_state_locked(task_id)
            for record in state.get("failed", []):
                key = str(record.get("key") or "")
                if key:
                    self._failed_keys.discard(key)
            state["recent"] = []
            state["failed"] = []
            self._save_state_locked()
            return {"status": "ok"}

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            self._load_locked()
            tasks_state: dict[str, Any] = {}
            for task in self._tasks:
                task_id = str(task.get("id") or "")
                state = copy.deepcopy(self._ensure_task_state_locked(task_id))
                state["running"] = self._is_watcher_running_locked(task_id)
                state["queue_size"] = int(self._queued_count.get(task_id, 0))
                state["active"] = [
                    copy.deepcopy(job)
                    for job in self._active_jobs.values()
                    if str(job.get("task_id")) == task_id
                ]
                tasks_state[task_id] = state
            return {
                "status": "ok",
                "queue_size": self._queue.qsize(),
                "worker_count": len([thread for thread in self._workers if thread.is_alive()]),
                "tasks": tasks_state,
            }

    def get_task_status(self, task_id: str) -> dict[str, Any]:
        status = self.get_status()
        if task_id not in status.get("tasks", {}):
            raise KeyError("任务不存在")
        return {"status": "ok", "task": status["tasks"][task_id]}

    def browse_115(self, cid: str = "0", drive_index: int = 0) -> dict[str, Any]:
        client = self.get_client(drive_index)
        cid = str(cid or "0").strip() or "0"
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

    def get_client(self, drive_index: int = 0):
        cookie = self._get_cookie(drive_index)
        return self.get_client_by_cookie(cookie)

    def get_client_by_cookie(self, cookie: str):
        cookie = str(cookie or "").strip()
        if not cookie:
            raise RuntimeError("Cookie 未配置")
        with self._lock:
            cached = self._client_cache.get(cookie)
            if cached:
                return cached
        try:
            client = P115Client(cookie, app="android")
        except TypeError:
            client = P115Client(cookie)
        with self._lock:
            self._client_cache[cookie] = client
        return client

    def browse_cloud_115(self, cookie: str, cid: str = "0", include_files: bool = True) -> dict[str, Any]:
        client = self.get_client_by_cookie(cookie)
        cid = str(cid or "0").strip() or "0"
        entries = self._list_cloud_115_children(client, cid, include_files=include_files, recursive=False)
        dirs = [entry for entry in entries if entry.get("type") == "dir"]
        files = [entry for entry in entries if entry.get("type") == "file"]
        return {"status": "ok", "dirs": dirs, "files": files}

    def start_cloud_rapid_transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        task = self._validate_cloud_rapid_payload(payload)
        job_id = f"cloud_rapid_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        now = int(time.time())
        job = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "message": "等待开始",
            "summary": "",
            "selected_count": len(task["items"]),
            "target_cid": task["target_cid"],
            "target_path": task["target_path"],
            "concurrency": task["concurrency"],
            "total_files": 0,
            "processed": 0,
            "success": 0,
            "skipped": 0,
            "failed": 0,
            "folders": 0,
            "retry_success": 0,
            "retry_failed": 0,
            "cache_hits": 0,
            "progress": 0,
            "current": "",
            "results": [],
            "truncated": False,
            "cancel_requested": False,
            "started_at": now,
            "updated_at": now,
            "finished_at": 0,
        }
        cancel_event = threading.Event()
        with self._lock:
            self._prune_cloud_rapid_jobs_locked()
            self._cloud_rapid_jobs[job_id] = job
            self._cloud_rapid_cancel_events[job_id] = cancel_event
        thread = threading.Thread(
            target=self._cloud_rapid_transfer_worker,
            args=(job_id, task),
            name=f"drive115-cloud-rapid-{job_id[-8:]}",
            daemon=True,
        )
        thread.start()
        return {"status": "ok", "job": self.get_cloud_rapid_job(job_id)}

    def cancel_cloud_rapid_job(self, job_id: str) -> dict[str, Any]:
        job_id = str(job_id or "")
        with self._lock:
            job = self._cloud_rapid_jobs.get(job_id)
            if not job:
                raise KeyError("任务不存在")
            if str(job.get("status") or "") in {"success", "partial", "error", "cancelled"}:
                return copy.deepcopy(job)
            job["cancel_requested"] = True
            job["status"] = "cancelling"
            job["stage"] = "cancelling"
            job["message"] = "正在取消网盘资源秒传"
            job["updated_at"] = int(time.time())
            event = self._cloud_rapid_cancel_events.get(job_id)
            if event:
                event.set()
            return copy.deepcopy(job)

    def get_cloud_rapid_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._cloud_rapid_jobs.get(str(job_id or ""))
            if not job:
                raise KeyError("任务不存在")
            return copy.deepcopy(job)

    def _is_cloud_rapid_cancelled(self, job_id: str | None) -> bool:
        if not job_id:
            return False
        with self._lock:
            job = self._cloud_rapid_jobs.get(str(job_id))
            event = self._cloud_rapid_cancel_events.get(str(job_id))
            return bool(
                (event and event.is_set())
                or (job and job.get("cancel_requested"))
                or (job and str(job.get("status") or "") == "cancelled")
            )

    def _raise_if_cloud_rapid_cancelled(self, job_id: str | None) -> None:
        if self._is_cloud_rapid_cancelled(job_id):
            raise CloudRapidCancelled("网盘资源秒传已取消")

    def _sleep_cloud_rapid_or_cancel(self, job_id: str | None, seconds: float) -> None:
        if not job_id:
            time.sleep(seconds)
            return
        with self._lock:
            event = self._cloud_rapid_cancel_events.get(str(job_id))
        if event and event.wait(max(0.0, seconds)):
            raise CloudRapidCancelled("网盘资源秒传已取消")
        self._raise_if_cloud_rapid_cancelled(job_id)

    def cloud_rapid_transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        task = self._validate_cloud_rapid_payload(payload)
        return self._run_cloud_rapid_transfer(task)

    def _validate_cloud_rapid_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_cookie = str(payload.get("source_cookie") or "").strip()
        target_cookie = str(payload.get("target_cookie") or "").strip()
        target_cid = str(payload.get("target_cid") or "").strip()
        target_path = str(payload.get("target_path") or "").strip()
        raw_items = payload.get("items") or []
        try:
            concurrency = int(payload.get("concurrency") or CLOUD_RAPID_DEFAULT_CONCURRENCY)
        except (TypeError, ValueError):
            concurrency = CLOUD_RAPID_DEFAULT_CONCURRENCY
        concurrency = max(1, min(CLOUD_RAPID_MAX_CONCURRENCY, concurrency))
        if not source_cookie:
            raise ValueError("请填写来源账号 CK")
        if not target_cookie:
            raise ValueError("请填写目标账号 CK")
        if not target_cid or not target_cid.isdigit() or target_cid == "0":
            raise ValueError("请选择目标网盘的非根目录")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("请选择需要秒传的文件或文件夹")

        items = []
        for raw_item in raw_items:
            item = self._normalize_cloud_selected_item(raw_item)
            if item:
                items.append(item)
        if not items:
            raise ValueError("请选择有效的文件或文件夹")
        return {
            "source_cookie": source_cookie,
            "target_cookie": target_cookie,
            "target_cid": target_cid,
            "target_path": target_path,
            "concurrency": concurrency,
            "items": items,
        }

    def _cloud_rapid_transfer_worker(self, job_id: str, task: dict[str, Any]) -> None:
        try:
            self._update_cloud_rapid_job(
                job_id,
                status="running",
                stage="scanning",
                message="正在连接 115 账号",
            )
            result = self._run_cloud_rapid_transfer(task, job_id=job_id)
            status = "success" if result.get("status") == "ok" else str(result.get("status") or "error")
            self._update_cloud_rapid_job(
                job_id,
                status=status,
                stage="finished" if status != "error" else "failed",
                message=str(result.get("summary") or "网盘资源秒传完成"),
                summary=str(result.get("summary") or ""),
                total_files=int(result.get("total_files") or 0),
                processed=int(result.get("processed") or 0),
                success=int(result.get("success") or 0),
                skipped=int(result.get("skipped") or 0),
                failed=int(result.get("failed") or 0),
                folders=int(result.get("folders") or 0),
                retry_success=int(result.get("retry_success") or 0),
                retry_failed=int(result.get("retry_failed") or 0),
                cache_hits=int(result.get("cache_hits") or 0),
                auth_cooldowns=int(result.get("auth_cooldowns") or 0),
                concurrency=int(result.get("concurrency") or task.get("concurrency") or CLOUD_RAPID_DEFAULT_CONCURRENCY),
                progress=100,
                finished_at=int(time.time()),
            )
        except CloudRapidCancelled as e:
            message = str(e) or "网盘资源秒传已取消"
            self._update_cloud_rapid_job(
                job_id,
                status="cancelled",
                stage="cancelled",
                message=message,
                summary=message,
                progress=100,
                finished_at=int(time.time()),
            )
        except Exception as e:
            logger.exception(f"[CloudRapid] 网盘资源秒传任务失败: {e}")
            self._update_cloud_rapid_job(
                job_id,
                status="error",
                stage="failed",
                message=str(e),
                summary=f"网盘资源秒传失败: {e}",
                finished_at=int(time.time()),
            )
        finally:
            with self._lock:
                self._cloud_rapid_cancel_events.pop(str(job_id), None)

    def _run_cloud_rapid_transfer(self, task: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
        self._raise_if_cloud_rapid_cancelled(job_id)
        source_client = self.get_client_by_cookie(task["source_cookie"])
        target_client = self.get_client_by_cookie(task["target_cookie"])
        self._raise_if_cloud_rapid_cancelled(job_id)
        target_cid = str(task.get("target_cid") or "")
        target_path = str(task.get("target_path") or "")
        concurrency = max(1, min(CLOUD_RAPID_MAX_CONCURRENCY, int(task.get("concurrency") or CLOUD_RAPID_DEFAULT_CONCURRENCY)))
        counters = {
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "folders": 0,
            "total_files": 0,
            "processed": 0,
            "retry_success": 0,
            "retry_failed": 0,
            "cache_hits": 0,
            "auth_cooldowns": 0,
        }
        results: list[dict[str, Any]] = []
        cache_db_path = CLOUD_RAPID_CACHE_DB
        server_db_path = self._cloud_server_db_path(job_id)
        dir_chain_cache: dict[tuple[str, str, str, str], str] = {}
        target_existing_cache: dict[str, set[str]] = {}
        auth_cooldown: dict[str, Any] = {"until": 0.0, "reason": ""}
        task_key = f"cloud_rapid:{job_id or uuid.uuid4().hex[:12]}"
        progress_lock = threading.Lock()
        dir_lock = threading.Lock()
        target_existing_lock = threading.Lock()
        auth_cooldown_lock = threading.Lock()

        self._init_cloud_rapid_cache_db(cache_db_path)
        self._raise_if_cloud_rapid_cancelled(job_id)
        self._reset_cloud_server_db(server_db_path, task)
        self._raise_if_cloud_rapid_cancelled(job_id)

        self._sync_cloud_rapid_job(
            job_id,
            counters,
            status="running",
            stage="indexing",
            message="正在建立来源索引库",
            concurrency=concurrency,
            server_db=str(server_db_path),
        )
        for item_index, item in enumerate(task["items"]):
            self._raise_if_cloud_rapid_cancelled(job_id)
            group_key = self._cloud_source_group_key(item, item_index)
            group_name = str(item.get("path") or item.get("name") or group_key)
            try:
                if item["type"] == "dir":
                    self._cloud_rapid_index_dir(
                        source_client,
                        item,
                        server_db_path,
                        counters,
                        group_key=group_key,
                        group_order=item_index,
                        group_name=group_name,
                        job_id=job_id,
                    )
                else:
                    self._cloud_rapid_index_file(
                        source_client,
                        item,
                        server_db_path,
                        counters,
                        group_key=group_key,
                        group_order=item_index,
                        group_name=group_name,
                        job_id=job_id,
                    )
            except CloudRapidCancelled:
                raise
            except Exception as e:
                self._record_cloud_rapid_result(
                    job_id,
                    counters,
                    results,
                    {
                        "status": "failed",
                        "type": item.get("type", "file"),
                        "name": item.get("name", ""),
                        "path": item.get("path", item.get("name", "")),
                        "message": str(e),
                    },
                    count_processed=False,
                )

        self._raise_if_cloud_rapid_cancelled(job_id)
        file_rows = self._cloud_load_server_file_rows(server_db_path)
        counters["total_files"] = len(file_rows)
        row_record_keys = [
            (row, self._cloud_upload_record_key(row, target_cid))
            for row in file_rows
        ]
        upload_statuses = self._cloud_load_upload_statuses(cache_db_path, [key for _, key in row_record_keys])
        failed_rows = [
            row
            for row, key in row_record_keys
            if str(upload_statuses.get(key) or "") == "failed"
        ]
        attempted_keys: set[str] = set()

        if failed_rows:
            self._raise_if_cloud_rapid_cancelled(job_id)
            self._sync_cloud_rapid_job(
                job_id,
                counters,
                status="running",
                stage="retrying",
                current="",
                message=f"发现历史失败 {len(failed_rows)} 个，先重试",
            )
            self._cloud_process_indexed_rows(
                source_client,
                target_client,
                failed_rows,
                target_cid,
                target_path,
                cache_db_path,
                counters,
                results,
                task_key,
                dir_chain_cache,
                dir_lock,
                target_existing_cache,
                target_existing_lock,
                auth_cooldown,
                auth_cooldown_lock,
                progress_lock,
                job_id,
                concurrency,
                retrying=True,
            )
            attempted_keys = {
                self._cloud_upload_record_key(row, target_cid)
                for row in failed_rows
            }

        self._raise_if_cloud_rapid_cancelled(job_id)
        fresh_statuses = self._cloud_load_upload_statuses(cache_db_path, [key for _, key in row_record_keys])
        remaining_rows = [
            row
            for row, key in row_record_keys
            if key not in attempted_keys and str(fresh_statuses.get(key) or "") != "success"
        ]
        already_done = len(file_rows) - len(failed_rows) - len(remaining_rows)

        if already_done > 0:
            self._sync_cloud_rapid_job(
                job_id,
                counters,
                status="running",
                stage="transferring",
                current="",
                message=f"跳过已有成功记录 {already_done} 个",
            )
            for row, key in row_record_keys:
                self._raise_if_cloud_rapid_cancelled(job_id)
                if key in attempted_keys or str(fresh_statuses.get(key) or "") != "success":
                    continue
                self._record_cloud_rapid_result(
                    job_id,
                    counters,
                    results,
                    {
                        "status": "success",
                        "type": "file",
                        "name": row.get("name", ""),
                        "path": row.get("path", row.get("name", "")),
                        "size": int(row.get("size") or 0),
                        "pickcode": str(row.get("pickcode") or ""),
                        "message": "已有成功记录，跳过重复秒传",
                    },
                )

        grouped_remaining = self._cloud_group_indexed_rows(remaining_rows)
        if grouped_remaining:
            self._raise_if_cloud_rapid_cancelled(job_id)
            for group_name, group_rows in grouped_remaining:
                self._raise_if_cloud_rapid_cancelled(job_id)
                self._sync_cloud_rapid_job(
                    job_id,
                    counters,
                    status="running",
                    stage="transferring",
                    current=group_name,
                    message=f"开始秒传来源: {group_name}，组内并发 {concurrency}",
                )
                self._cloud_process_indexed_rows(
                    source_client,
                    target_client,
                    group_rows,
                    target_cid,
                    target_path,
                    cache_db_path,
                    counters,
                    results,
                    task_key,
                    dir_chain_cache,
                    dir_lock,
                    target_existing_cache,
                    target_existing_lock,
                    auth_cooldown,
                    auth_cooldown_lock,
                    progress_lock,
                    job_id,
                    concurrency,
                    retrying=False,
                )

        status = "ok" if counters["failed"] == 0 and counters["skipped"] == 0 else (
            "partial" if counters["success"] or counters["skipped"] else "error"
        )
        summary = (
            f"总文件 {counters['total_files']}，成功 {counters['success']}，"
            f"跳过 {counters['skipped']}，失败 {counters['failed']}，目录 {counters['folders']}，"
            f"重试成功 {counters['retry_success']}，URL缓存命中 {counters['cache_hits']}，"
            f"封控冷却 {counters['auth_cooldowns']} 次，并发 {concurrency}"
        )
        return {
            "status": status,
            "summary": summary,
            "success": counters["success"],
            "failed": counters["failed"],
            "skipped": counters["skipped"],
            "folders": counters["folders"],
            "retry_success": counters["retry_success"],
            "retry_failed": counters["retry_failed"],
            "cache_hits": counters["cache_hits"],
            "auth_cooldowns": counters["auth_cooldowns"],
            "total_files": counters["total_files"],
            "processed": counters["processed"],
            "concurrency": concurrency,
            "results": self._limit_cloud_rapid_results(results),
            "truncated": len(results) > CLOUD_RAPID_RESULT_LIMIT,
        }

    def _prune_cloud_rapid_jobs_locked(self) -> None:
        now = int(time.time())
        finished = [
            (str(job_id), int(job.get("finished_at") or job.get("updated_at") or 0))
            for job_id, job in self._cloud_rapid_jobs.items()
            if str(job.get("status") or "") in {"success", "partial", "error", "cancelled"}
        ]
        for job_id, finished_at in finished:
            if finished_at and now - finished_at > CLOUD_RAPID_JOB_KEEP_SECONDS:
                self._cloud_rapid_jobs.pop(job_id, None)
        if len(self._cloud_rapid_jobs) <= CLOUD_RAPID_JOB_HISTORY_LIMIT:
            return
        removable = sorted(
            [
                (str(job_id), int(job.get("finished_at") or job.get("updated_at") or 0))
                for job_id, job in self._cloud_rapid_jobs.items()
                if str(job.get("status") or "") in {"success", "partial", "error", "cancelled"}
            ],
            key=lambda item: item[1],
        )
        while len(self._cloud_rapid_jobs) > CLOUD_RAPID_JOB_HISTORY_LIMIT and removable:
            job_id, _ = removable.pop(0)
            self._cloud_rapid_jobs.pop(job_id, None)

    def _update_cloud_rapid_job(self, job_id: str | None, **updates: Any) -> None:
        if not job_id:
            return
        with self._lock:
            job = self._cloud_rapid_jobs.get(str(job_id))
            if not job:
                return
            if job.get("cancel_requested") and str(job.get("status") or "") == "cancelling":
                next_updates = dict(updates)
                if str(next_updates.get("status") or "") in {"queued", "running"}:
                    next_updates["status"] = "cancelling"
                if str(next_updates.get("stage") or "") not in {"cancelled", "cancelling"}:
                    next_updates["stage"] = "cancelling"
                if str(next_updates.get("message") or "").strip() in {"", "正在连接 115 账号"}:
                    next_updates["message"] = "正在取消网盘资源秒传"
                updates = next_updates
            job.update(updates)
            job["updated_at"] = int(time.time())
            if "progress" not in updates:
                total = int(job.get("total_files") or 0)
                processed = int(job.get("processed") or 0)
                if total > 0:
                    progress = int(processed / total * 100)
                    if str(job.get("status") or "") in {"running", "queued"}:
                        progress = min(progress, 99)
                    job["progress"] = max(0, min(100, progress))

    def _sync_cloud_rapid_job(self, job_id: str | None, counters: dict[str, int], **updates: Any) -> None:
        if not job_id:
            return
        fields = {
            "total_files": int(counters.get("total_files") or 0),
            "processed": int(counters.get("processed") or 0),
            "success": int(counters.get("success") or 0),
            "skipped": int(counters.get("skipped") or 0),
            "failed": int(counters.get("failed") or 0),
            "folders": int(counters.get("folders") or 0),
            "retry_success": int(counters.get("retry_success") or 0),
            "retry_failed": int(counters.get("retry_failed") or 0),
            "cache_hits": int(counters.get("cache_hits") or 0),
            "auth_cooldowns": int(counters.get("auth_cooldowns") or 0),
        }
        fields.update(updates)
        self._update_cloud_rapid_job(job_id, **fields)

    def _record_cloud_rapid_result(
        self,
        job_id: str | None,
        counters: dict[str, int],
        results: list[dict[str, Any]],
        record: dict[str, Any],
        *,
        count_processed: bool = True,
        progress_lock: threading.Lock | None = None,
    ) -> None:
        if progress_lock:
            with progress_lock:
                self._record_cloud_rapid_result(
                    job_id,
                    counters,
                    results,
                    record,
                    count_processed=count_processed,
                    progress_lock=None,
                )
            return
        status = str(record.get("status") or "")
        if status in {"success", "failed", "skipped"}:
            counters[status] = int(counters.get(status) or 0) + 1
        if record.get("retrying"):
            if status == "success":
                counters["retry_success"] = int(counters.get("retry_success") or 0) + 1
            elif status == "failed":
                counters["retry_failed"] = int(counters.get("retry_failed") or 0) + 1
        if count_processed:
            counters["processed"] = int(counters.get("processed") or 0) + 1
        results.append(record)
        if job_id:
            with self._lock:
                job = self._cloud_rapid_jobs.get(str(job_id))
                if job:
                    job_results = job.setdefault("results", [])
                    record_copy = copy.deepcopy(record)
                    if len(job_results) < CLOUD_RAPID_RESULT_LIMIT:
                        job_results.append(record_copy)
                    else:
                        job["truncated"] = True
                        worst_idx = max(
                            range(len(job_results)),
                            key=lambda idx: self._cloud_result_rank(job_results[idx]),
                        )
                        if self._cloud_result_rank(record_copy) < self._cloud_result_rank(job_results[worst_idx]):
                            job_results[worst_idx] = record_copy
            self._sync_cloud_rapid_job(
                job_id,
                counters,
                status="running",
                stage="retrying" if record.get("retrying") else "transferring",
                current=str(record.get("path") or record.get("name") or ""),
                message=str(record.get("message") or ""),
            )

    def _cloud_result_rank(self, record: dict[str, Any]) -> int:
        return {
            "failed": 0,
            "skipped": 1,
            "success": 2,
        }.get(str(record.get("status") or ""), 3)

    def _limit_cloud_rapid_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ranked = sorted(enumerate(results), key=lambda item: (self._cloud_result_rank(item[1]), item[0]))
        selected = sorted(ranked[:CLOUD_RAPID_RESULT_LIMIT], key=lambda item: item[0])
        return [record for _, record in selected]

    def _normalize_cloud_selected_item(self, raw_item: Any) -> dict[str, Any] | None:
        if not isinstance(raw_item, dict):
            return None
        item_type = "dir" if str(raw_item.get("type") or "").lower() == "dir" else "file"
        item_id = str(raw_item.get("id") or raw_item.get("cid") or raw_item.get("file_id") or "").strip()
        if not item_id:
            return None
        name = str(raw_item.get("name") or "").strip() or item_id
        return {
            "type": item_type,
            "id": item_id,
            "cid": str(raw_item.get("cid") or item_id).strip(),
            "file_id": str(raw_item.get("file_id") or item_id).strip(),
            "name": name,
            "path": str(raw_item.get("path") or name).strip() or name,
            "pickcode": str(raw_item.get("pickcode") or raw_item.get("pick_code") or "").strip(),
            "sha1": str(raw_item.get("sha1") or "").strip().upper(),
            "size": int(raw_item.get("size") or 0),
            "is_collect": self._cloud_to_bool(raw_item.get("is_collect")),
        }

    def _fetch_cloud_115_children_page(
        self,
        client,
        payload: dict[str, Any],
        *,
        webapi_only: bool = False,
    ) -> dict[str, Any]:
        if not webapi_only:
            try:
                resp = client.fs_files_app(
                    payload,
                    app="android",
                    base_url="https://proapi.115.com",
                    headers={"user-agent": CLOUD_RAPID_UA},
                    timeout=20,
                )
                if isinstance(resp, dict) and resp.get("state"):
                    return resp
                logger.warning(f"[CloudRapid] proapi 目录读取失败，切换 webapi: {resp}")
            except Exception as e:
                logger.warning(f"[CloudRapid] proapi 目录读取异常，切换 webapi: {e}")
        resp = client.fs_files(payload, timeout=20)
        if not isinstance(resp, dict) or not resp.get("state"):
            raise RuntimeError("读取 115 目录失败")
        return resp

    def _list_cloud_115_children(self, client, cid: str, include_files: bool, recursive: bool) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        offset = 0
        cid = str(cid or "0").strip() or "0"
        while True:
            payload = {
                "cid": int(cid),
                "limit": CLOUD_RAPID_LIST_LIMIT,
                "offset": offset,
                "type": 0,
                "fc_mix": 1 if include_files else 0,
            }
            resp = self._fetch_cloud_115_children_page(client, payload, webapi_only=True)
            data = self._extract_115_list_data(resp)
            if not data:
                break
            for raw in data:
                entry = self._normalize_cloud_115_entry(raw)
                if not entry:
                    continue
                if entry["type"] == "dir" or include_files:
                    entries.append(entry)
            if not recursive or len(data) < CLOUD_RAPID_LIST_LIMIT:
                break
            offset += CLOUD_RAPID_LIST_LIMIT
        return entries

    def _extract_115_list_data(self, resp: dict[str, Any]) -> list:
        data = resp.get("data", [])
        if isinstance(data, dict):
            data = data.get("list") or data.get("files") or data.get("data") or data.get("items") or []
        return data if isinstance(data, list) else []

    def _normalize_cloud_115_entry(self, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        name = str(
            item.get("fn")
            or item.get("n")
            or item.get("name")
            or item.get("file_name")
            or ""
        ).strip()
        item_id = str(
            item.get("fid")
            or item.get("id")
            or item.get("cid")
            or item.get("file_id")
            or item.get("category_id")
            or ""
        ).strip()
        if not item_id:
            return None
        is_dir = (
            item.get("is_dir") is True
            or str(item.get("fc", "") or "") == "0"
            or ("fid" not in item and ("cid" in item or "category_id" in item))
        )
        if is_dir:
            return {
                "type": "dir",
                "id": item_id,
                "cid": item_id,
                "name": name or item_id,
            }
        size = int(item.get("fs") or item.get("s") or item.get("size") or item.get("file_size") or 0)
        return {
            "type": "file",
            "id": item_id,
            "file_id": item_id,
            "name": name or item_id,
            "size": size,
            "pickcode": str(item.get("pc") or item.get("pickcode") or item.get("pick_code") or "").strip(),
            "sha1": str(item.get("sha1") or item.get("sha") or "").strip().upper(),
            "is_collect": self._cloud_to_bool(item.get("is_collect")),
        }

    def _list_cloud_115_tree_entries(self, client, cid: str) -> list[dict[str, Any]]:
        return list(self._iter_cloud_115_tree_entries(client, cid))

    def _iter_cloud_115_tree_entries(self, client, cid: str):
        entries = iter_files_with_path(
            client,
            int(str(cid or "0").strip() or 0),
            page_size=1150,
            type=99,
            normalize_attr=normalize_attr_simple,
            with_ancestors=True,
            app="web",
            max_workers=0,
            cooldown=0.2,
            timeout=30,
        )
        for raw in entries:
            entry = self._normalize_cloud_115_entry(raw)
            if not entry:
                continue
            relpath = self._cloud_tree_entry_relpath(raw, entry)
            entry["path"] = relpath or entry.get("name", "")
            entry["relpath"] = relpath or entry.get("name", "")
            entry["parent_id"] = str(raw.get("parent_id") or raw.get("pid") or "")
            yield entry

    def _cloud_tree_entry_relpath(self, raw: dict[str, Any], entry: dict[str, Any]) -> str:
        relpath = str(raw.get("relpath") or "").strip("/")
        if relpath:
            return relpath
        path = str(raw.get("path") or "").strip("/")
        top_path = str(raw.get("top_path") or "").strip("/")
        if path and top_path:
            if path == top_path:
                return ""
            prefix = top_path.rstrip("/") + "/"
            if path.startswith(prefix):
                return path[len(prefix):].strip("/")
        name = str(entry.get("name") or raw.get("name") or "").strip()
        if path and name and path.endswith("/" + name):
            return name
        if path:
            return path
        return name

    def _cloud_server_db_path(self, job_id: str | None) -> Path:
        raw_id = str(job_id or f"direct_{int(time.time())}_{uuid.uuid4().hex[:8]}")
        safe_id = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw_id)
        return Path("config") / f"{CLOUD_RAPID_SERVER_DB_PREFIX}_{safe_id}.db"

    def _connect_cloud_sqlite(self, path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def _cloud_sqlite(self, path: Path):
        conn = self._connect_cloud_sqlite(path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_cloud_rapid_cache_db(self, db_path: Path) -> None:
        with self._cloud_sqlite(db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS url_cache (
                    pickcode TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    sha1 TEXT,
                    filename TEXT,
                    timestamp INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_records (
                    record_key TEXT PRIMARY KEY,
                    pickcode TEXT,
                    filename TEXT,
                    size INTEGER NOT NULL DEFAULT 0,
                    sha1 TEXT,
                    source_path TEXT,
                    target_cid TEXT,
                    status TEXT NOT NULL,
                    error_msg TEXT,
                    upload_time INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS directory_completion (
                    task_key TEXT NOT NULL,
                    path TEXT NOT NULL,
                    target_cid TEXT,
                    completed_at INTEGER NOT NULL,
                    PRIMARY KEY (task_key, path)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_records_pickcode ON upload_records(pickcode)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_records_status ON upload_records(status)")

    def _reset_cloud_server_db(self, db_path: Path, task: dict[str, Any]) -> None:
        if db_path.exists():
            try:
                db_path.unlink()
            except OSError:
                pass
        with self._cloud_sqlite(db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS directories (
                    path TEXT PRIMARY KEY,
                    parent_path TEXT,
                    name TEXT,
                    cid TEXT,
                    source_cid TEXT,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cid TEXT,
                    file_id TEXT,
                    name TEXT NOT NULL,
                    sha1 TEXT,
                    size INTEGER NOT NULL DEFAULT 0,
                    pickcode TEXT,
                    path TEXT NOT NULL,
                    parent_path TEXT,
                    source_cid TEXT,
                    group_key TEXT,
                    group_order INTEGER NOT NULL DEFAULT 0,
                    group_name TEXT,
                    is_collect INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cloud_server_files_unique ON files(group_key, path, pickcode)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_server_files_pickcode ON files(pickcode)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_server_files_group ON files(group_order, id)")
            now = int(time.time())
            conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value, updated_at) VALUES (?, ?, ?)",
                ("task", json.dumps(task, ensure_ascii=False), now),
            )

    def _normalize_cloud_relpath(self, path: Any) -> str:
        parts = []
        for part in str(path or "").replace("\\", "/").split("/"):
            clean = part.strip().strip("/")
            if clean and clean != ".":
                parts.append(clean)
        return "/".join(parts)

    def _cloud_join_relpath(self, *parts: Any) -> str:
        return self._normalize_cloud_relpath("/".join(str(part or "") for part in parts if str(part or "").strip()))

    def _cloud_to_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    def _cloud_source_group_key(self, item: dict[str, Any], index: int) -> str:
        raw = "|".join([
            str(index),
            str(item.get("type") or ""),
            str(item.get("cid") or item.get("id") or item.get("file_id") or ""),
            str(item.get("pickcode") or item.get("pick_code") or ""),
            self._normalize_cloud_relpath(item.get("path") or item.get("name") or ""),
        ])
        return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]

    def _cloud_insert_server_directory(
        self,
        db_path: Path,
        path: str,
        name: str,
        cid: str = "",
        source_cid: str = "",
    ) -> bool:
        normalized_path = self._normalize_cloud_relpath(path)
        if not normalized_path:
            return False
        parent_path = self._normalize_cloud_relpath(posixpath.dirname(normalized_path))
        with self._cloud_sqlite(db_path) as conn:
            return self._cloud_insert_server_directory_conn(
                conn,
                normalized_path,
                parent_path,
                name,
                cid=cid,
                source_cid=source_cid,
            )

    def _cloud_insert_server_directory_conn(
        self,
        conn: sqlite3.Connection,
        normalized_path: str,
        parent_path: str,
        name: str,
        cid: str = "",
        source_cid: str = "",
    ) -> bool:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO directories(path, parent_path, name, cid, source_cid, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_path,
                parent_path,
                str(name or ""),
                str(cid or ""),
                str(source_cid or ""),
                int(time.time()),
            ),
        )
        return cursor.rowcount > 0

    def _cloud_insert_server_file(self, db_path: Path, item: dict[str, Any], path: str, parent_path: str) -> bool:
        normalized_path = self._normalize_cloud_relpath(path)
        if not normalized_path:
            normalized_path = self._normalize_cloud_relpath(item.get("name") or item.get("file_id") or item.get("id") or "")
        filename = str(item.get("name") or posixpath.basename(normalized_path) or "video.mkv")
        with self._cloud_sqlite(db_path) as conn:
            return self._cloud_insert_server_file_conn(
                conn,
                item,
                normalized_path,
                self._normalize_cloud_relpath(parent_path),
                filename,
            )

    def _cloud_insert_server_file_conn(
        self,
        conn: sqlite3.Connection,
        item: dict[str, Any],
        normalized_path: str,
        parent_path: str,
        filename: str,
    ) -> bool:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO files(
                cid, file_id, name, sha1, size, pickcode, path, parent_path, source_cid,
                group_key, group_order, group_name, is_collect, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.get("cid") or ""),
                str(item.get("file_id") or item.get("id") or ""),
                filename,
                str(item.get("sha1") or "").strip().upper(),
                int(item.get("size") or 0),
                str(item.get("pickcode") or item.get("pick_code") or "").strip(),
                normalized_path,
                parent_path,
                str(item.get("source_cid") or item.get("parent_id") or ""),
                str(item.get("_group_key") or ""),
                int(item.get("_group_order") or 0),
                str(item.get("_group_name") or ""),
                1 if self._cloud_to_bool(item.get("is_collect")) else 0,
                int(time.time()),
            ),
        )
        return cursor.rowcount > 0

    def _cloud_rapid_index_dir(
        self,
        source_client,
        item: dict[str, Any],
        server_db_path: Path,
        counters: dict[str, int],
        group_key: str = "",
        group_order: int = 0,
        group_name: str = "",
        job_id: str | None = None,
    ) -> None:
        self._raise_if_cloud_rapid_cancelled(job_id)
        if CLOUD_RAPID_MAX_FILES > 0 and counters["total_files"] >= CLOUD_RAPID_MAX_FILES:
            raise RuntimeError(f"本次文件数超过上限 {CLOUD_RAPID_MAX_FILES}")
        dir_name = str(item.get("name") or item.get("cid") or "未命名目录")
        root_path = self._normalize_cloud_relpath(dir_name)
        self._sync_cloud_rapid_job(
            job_id,
            counters,
            status="running",
            stage="indexing",
            current=root_path,
            message=f"正在索引来源目录: {dir_name}",
        )
        last_sync_at = 0.0
        pending_writes = 0

        def sync_index_progress(current: str, message: str, *, force: bool = False) -> None:
            nonlocal last_sync_at
            now = time.monotonic()
            if force or now - last_sync_at >= 1:
                last_sync_at = now
                self._sync_cloud_rapid_job(
                    job_id,
                    counters,
                    status="running",
                    stage="indexing",
                    current=current,
                    message=message,
                )

        with self._cloud_sqlite(server_db_path) as conn:
            if self._cloud_insert_server_directory_conn(
                conn,
                root_path,
                self._normalize_cloud_relpath(posixpath.dirname(root_path)),
                dir_name,
                cid=str(item.get("cid") or item.get("id") or ""),
                source_cid=str(item.get("cid") or item.get("id") or ""),
            ):
                counters["folders"] += 1
                pending_writes += 1

            self._sleep_cloud_rapid_or_cancel(job_id, 2)
            for child in self._iter_cloud_115_tree_entries(source_client, str(item.get("cid") or item.get("id"))):
                self._raise_if_cloud_rapid_cancelled(job_id)
                child["_group_key"] = group_key
                child["_group_order"] = group_order
                child["_group_name"] = group_name or dir_name
                if child.get("type") == "dir":
                    rel_dir = self._normalize_cloud_relpath(child.get("relpath") or child.get("path") or child.get("name") or "")
                    if not rel_dir:
                        continue
                    dir_path = self._cloud_join_relpath(root_path, rel_dir)
                    if self._cloud_insert_server_directory_conn(
                        conn,
                        dir_path,
                        self._normalize_cloud_relpath(posixpath.dirname(dir_path)),
                        posixpath.basename(dir_path),
                        cid=str(child.get("cid") or child.get("id") or ""),
                        source_cid=str(child.get("cid") or child.get("id") or ""),
                    ):
                        counters["folders"] += 1
                        pending_writes += 1
                        sync_index_progress(dir_path, f"已索引目录: {dir_path}")
                else:
                    if CLOUD_RAPID_MAX_FILES > 0 and counters["total_files"] >= CLOUD_RAPID_MAX_FILES:
                        raise RuntimeError(f"本次文件数超过上限 {CLOUD_RAPID_MAX_FILES}")
                    rel_file = self._normalize_cloud_relpath(child.get("relpath") or child.get("path") or child.get("name") or "")
                    file_path = self._cloud_join_relpath(root_path, rel_file)
                    parent_path = self._normalize_cloud_relpath(posixpath.dirname(file_path))
                    filename = str(child.get("name") or posixpath.basename(file_path) or "video.mkv")
                    if self._cloud_insert_server_file_conn(conn, child, file_path, parent_path, filename):
                        counters["total_files"] += 1
                        pending_writes += 1
                        sync_index_progress(file_path, f"已索引文件: {filename}")

                if pending_writes >= 5000:
                    conn.commit()
                    pending_writes = 0

            conn.commit()

        sync_index_progress(root_path, f"来源目录索引完成: {dir_name}", force=True)

    def _cloud_rapid_index_file(
        self,
        source_client,
        item: dict[str, Any],
        server_db_path: Path,
        counters: dict[str, int],
        group_key: str = "",
        group_order: int = 0,
        group_name: str = "",
        job_id: str | None = None,
    ) -> None:
        self._raise_if_cloud_rapid_cancelled(job_id)
        if CLOUD_RAPID_MAX_FILES > 0 and counters["total_files"] >= CLOUD_RAPID_MAX_FILES:
            raise RuntimeError(f"本次文件数超过上限 {CLOUD_RAPID_MAX_FILES}")
        info = self._resolve_cloud_source_file_info(source_client, item)
        filename = str(info.get("name") or item.get("name") or "video.mkv")
        indexed = dict(item)
        indexed.update(info)
        indexed["name"] = filename
        indexed["_group_key"] = group_key
        indexed["_group_order"] = group_order
        indexed["_group_name"] = group_name or filename
        file_path = self._normalize_cloud_relpath(filename)
        if self._cloud_insert_server_file(server_db_path, indexed, file_path, ""):
            counters["total_files"] += 1
        self._sync_cloud_rapid_job(
            job_id,
            counters,
            status="running",
            stage="indexing",
            current=file_path,
            message=f"已索引文件: {filename}",
        )

    def _cloud_load_server_file_rows(self, db_path: Path) -> list[dict[str, Any]]:
        with self._cloud_sqlite(db_path) as conn:
            rows = conn.execute("SELECT * FROM files ORDER BY group_order, id").fetchall()
        return [dict(row) for row in rows]

    def _cloud_group_indexed_rows(self, rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
        groups: dict[str, dict[str, Any]] = {}
        for idx, row in enumerate(rows):
            key = str(row.get("group_key") or f"legacy-{idx}")
            group = groups.setdefault(
                key,
                {
                    "order": int(row.get("group_order") or 0),
                    "name": str(row.get("group_name") or row.get("path") or row.get("name") or key),
                    "rows": [],
                },
            )
            group["rows"].append(row)
        ordered = sorted(groups.values(), key=lambda item: (int(item["order"]), str(item["name"])))
        return [(str(item["name"]), list(item["rows"])) for item in ordered]

    def _cloud_upload_record_key(self, row: dict[str, Any], target_base_cid: str) -> str:
        raw = "|".join([
            str(target_base_cid or ""),
            self._normalize_cloud_relpath(row.get("path") or row.get("name") or ""),
            str(row.get("pickcode") or row.get("sha1") or row.get("file_id") or ""),
        ])
        return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()

    def _cloud_load_upload_statuses(self, db_path: Path, record_keys: list[str]) -> dict[str, str]:
        if not record_keys:
            return {}
        self._init_cloud_rapid_cache_db(db_path)
        statuses: dict[str, str] = {}
        unique_keys = list(dict.fromkeys(str(key) for key in record_keys if key))
        with self._cloud_sqlite(db_path) as conn:
            for i in range(0, len(unique_keys), 800):
                batch = unique_keys[i:i + 800]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT record_key, status FROM upload_records WHERE record_key IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    statuses[str(row["record_key"])] = str(row["status"] or "")
        return statuses

    def _cloud_record_upload_result(
        self,
        db_path: Path,
        record_key: str,
        row: dict[str, Any],
        target_cid: str,
        status: str,
        error_msg: str = "",
    ) -> None:
        self._init_cloud_rapid_cache_db(db_path)
        now = int(time.time())
        with self._cloud_sqlite(db_path) as conn:
            existing = conn.execute(
                "SELECT created_at FROM upload_records WHERE record_key = ?",
                (record_key,),
            ).fetchone()
            created_at = int(existing["created_at"]) if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO upload_records(
                    record_key, pickcode, filename, size, sha1, source_path, target_cid,
                    status, error_msg, upload_time, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_key,
                    str(row.get("pickcode") or ""),
                    str(row.get("name") or ""),
                    int(row.get("size") or 0),
                    str(row.get("sha1") or "").upper(),
                    self._normalize_cloud_relpath(row.get("path") or row.get("name") or ""),
                    str(target_cid or ""),
                    status,
                    str(error_msg or ""),
                    now if status == "success" else 0,
                    created_at,
                    now,
                ),
            )

    def _cloud_mark_directory_completion(self, db_path: Path, task_key: str, path: str, target_cid: str) -> None:
        self._init_cloud_rapid_cache_db(db_path)
        with self._cloud_sqlite(db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO directory_completion(task_key, path, target_cid, completed_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(task_key or ""), self._normalize_cloud_relpath(path), str(target_cid or ""), int(time.time())),
            )

    def _cloud_process_indexed_rows(
        self,
        source_client,
        target_client,
        rows: list[dict[str, Any]],
        target_base_cid: str,
        target_base_path: str,
        cache_db_path: Path,
        counters: dict[str, int],
        results: list[dict[str, Any]],
        task_key: str,
        dir_chain_cache: dict,
        dir_lock: threading.Lock,
        target_existing_cache: dict[str, set[str]],
        target_existing_lock: threading.Lock,
        auth_cooldown: dict[str, Any],
        auth_cooldown_lock: threading.Lock,
        progress_lock: threading.Lock,
        job_id: str | None,
        concurrency: int,
        retrying: bool = False,
    ) -> None:
        self._raise_if_cloud_rapid_cancelled(job_id)
        if concurrency <= 1 or len(rows) <= 1:
            for row in rows:
                self._raise_if_cloud_rapid_cancelled(job_id)
                self._execute_cloud_rapid_index_row(
                    source_client,
                    target_client,
                    row,
                    target_base_cid,
                    target_base_path,
                    cache_db_path,
                    counters,
                    results,
                    task_key,
                    dir_chain_cache,
                    dir_lock,
                    target_existing_cache,
                    target_existing_lock,
                    auth_cooldown,
                    auth_cooldown_lock,
                    progress_lock,
                    job_id,
                    retrying=retrying,
                )
            return

        row_iter = iter(rows)
        pending = set()
        executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="cloud-rapid-file")
        wait_for_running = True

        def submit_available() -> None:
            while len(pending) < concurrency:
                self._raise_if_cloud_rapid_cancelled(job_id)
                try:
                    row = next(row_iter)
                except StopIteration:
                    return
                pending.add(executor.submit(
                    self._execute_cloud_rapid_index_row,
                    source_client,
                    target_client,
                    row,
                    target_base_cid,
                    target_base_path,
                    cache_db_path,
                    counters,
                    results,
                    task_key,
                    dir_chain_cache,
                    dir_lock,
                    target_existing_cache,
                    target_existing_lock,
                    auth_cooldown,
                    auth_cooldown_lock,
                    progress_lock,
                    job_id,
                    retrying,
                ))

        try:
            submit_available()
            while pending:
                self._raise_if_cloud_rapid_cancelled(job_id)
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    future.result()
                submit_available()
        except CloudRapidCancelled:
            wait_for_running = True
            for future in pending:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=wait_for_running, cancel_futures=True)

    def _execute_cloud_rapid_index_row(
        self,
        source_client,
        target_client,
        row: dict[str, Any],
        target_base_cid: str,
        target_base_path: str,
        cache_db_path: Path,
        counters: dict[str, int],
        results: list[dict[str, Any]],
        task_key: str,
        dir_chain_cache: dict,
        dir_lock: threading.Lock,
        target_existing_cache: dict[str, set[str]],
        target_existing_lock: threading.Lock,
        auth_cooldown: dict[str, Any],
        auth_cooldown_lock: threading.Lock,
        progress_lock: threading.Lock,
        job_id: str | None,
        retrying: bool = False,
    ) -> None:
        self._raise_if_cloud_rapid_cancelled(job_id)
        record_key = self._cloud_upload_record_key(row, target_base_cid)
        try:
            self._cloud_rapid_transfer_index_row(
                source_client,
                target_client,
                row,
                target_base_cid,
                target_base_path,
                cache_db_path,
                record_key,
                counters,
                results,
                task_key,
                dir_chain_cache,
                dir_lock,
                target_existing_cache,
                target_existing_lock,
                auth_cooldown,
                auth_cooldown_lock,
                progress_lock,
                job_id,
                retrying=retrying,
            )
        except CloudRapidCancelled:
            raise
        except Exception as e:
            self._cloud_record_upload_result(cache_db_path, record_key, row, target_base_cid, "failed", str(e))
            self._record_cloud_rapid_result(
                job_id,
                counters,
                results,
                {
                    "status": "failed",
                    "type": "file",
                    "name": row.get("name", ""),
                    "path": row.get("path", row.get("name", "")),
                    "size": int(row.get("size") or 0),
                    "pickcode": str(row.get("pickcode") or ""),
                    "message": str(e),
                    "retrying": retrying,
                },
                progress_lock=progress_lock,
            )

    def _cloud_rapid_transfer_index_row(
        self,
        source_client,
        target_client,
        row: dict[str, Any],
        target_base_cid: str,
        target_base_path: str,
        cache_db_path: Path,
        record_key: str,
        counters: dict[str, int],
        results: list[dict[str, Any]],
        task_key: str,
        dir_chain_cache: dict,
        dir_lock: threading.Lock,
        target_existing_cache: dict[str, set[str]],
        target_existing_lock: threading.Lock,
        auth_cooldown: dict[str, Any],
        auth_cooldown_lock: threading.Lock,
        progress_lock: threading.Lock | None,
        job_id: str | None = None,
        retrying: bool = False,
    ) -> None:
        self._raise_if_cloud_rapid_cancelled(job_id)
        display_path = self._normalize_cloud_relpath(row.get("path") or row.get("name") or "")
        filename = str(row.get("name") or posixpath.basename(display_path) or "video.mkv")
        self._sync_cloud_rapid_job(
            job_id,
            counters,
            status="running",
            stage="retrying" if retrying else "transferring",
            current=display_path or filename,
            message=f"{'重试' if retrying else '准备'}秒传: {filename}",
        )
        status_map = self._cloud_load_upload_statuses(cache_db_path, [record_key])
        if str(status_map.get(record_key) or "") == "success":
            self._record_cloud_rapid_result(
                job_id,
                counters,
                results,
                {
                    "status": "success",
                    "type": "file",
                    "name": filename,
                    "path": display_path or filename,
                    "size": int(row.get("size") or 0),
                    "pickcode": str(row.get("pickcode") or ""),
                    "message": "已有成功记录，跳过重复秒传",
                    "retrying": retrying,
                },
                progress_lock=progress_lock,
            )
            return

        self._raise_if_cloud_rapid_cancelled(job_id)
        info = dict(row)
        if not info.get("pickcode") or not info.get("sha1") or not int(info.get("size") or 0):
            info = self._resolve_cloud_source_file_info(source_client, info)
            row.update(info)
            filename = str(info.get("name") or filename)
        pickcode = str(info.get("pickcode") or "").strip()
        sha1 = str(info.get("sha1") or "").strip().upper()
        size = int(info.get("size") or 0)
        if not pickcode:
            raise RuntimeError(f"缺少 pickcode: {filename}")
        if self._cloud_to_bool(info.get("is_collect")) and size >= CLOUD_RAPID_COLLECT_SKIP_SIZE:
            self._cloud_record_upload_result(
                cache_db_path,
                record_key,
                row,
                target_base_cid,
                "skipped",
                "合集文件超过 115MB，按 PyPush 逻辑跳过",
            )
            self._record_cloud_rapid_result(
                job_id,
                counters,
                results,
                {
                    "status": "skipped",
                    "type": "file",
                    "name": filename,
                    "path": display_path or filename,
                    "size": size,
                    "pickcode": pickcode,
                    "message": "合集文件超过 115MB，按 PyPush 逻辑跳过",
                    "retrying": retrying,
                },
                progress_lock=progress_lock,
            )
            return
        if not sha1 or not size:
            raise RuntimeError(f"缺少 SHA1 或大小: {filename}")

        self._raise_if_cloud_rapid_cancelled(job_id)
        parent_path = self._normalize_cloud_relpath(row.get("parent_path") or posixpath.dirname(display_path))
        target_dir_cid = self._ensure_cloud_rapid_target_dir(
            target_client,
            target_base_cid,
            target_base_path,
            parent_path,
            counters,
            task_key,
            dir_chain_cache,
            dir_lock,
            job_id,
        )
        self._raise_if_cloud_rapid_cancelled(job_id)
        download_url, cache_hit = self._get_cloud_download_url_cached(source_client, cache_db_path, pickcode, info)
        if cache_hit:
            with progress_lock:
                counters["cache_hits"] = int(counters.get("cache_hits") or 0) + 1
        if not download_url:
            raise RuntimeError(f"无法获取来源直链: {filename}")
        range_retry_used = False

        def read_range_callback(sign_check: str) -> bytes:
            nonlocal download_url, range_retry_used
            self._raise_if_cloud_rapid_cancelled(job_id)
            try:
                return self._read_cloud_range_bytes(download_url, sign_check, job_id=job_id)
            except Exception:
                if range_retry_used:
                    raise
                range_retry_used = True
                self._raise_if_cloud_rapid_cancelled(job_id)
                logger.warning(f"[CloudRapid] range 校验失败，刷新来源直链后重试: {pickcode}")
                fresh_url = self._refresh_cloud_download_url(source_client, cache_db_path, pickcode, info)
                if not fresh_url:
                    raise
                download_url = fresh_url
                return self._read_cloud_range_bytes(download_url, sign_check, job_id=job_id)

        try:
            self._raise_if_cloud_rapid_cancelled(job_id)
            result = target_client.upload_file_init(
                filename=filename,
                filesize=size,
                filesha1=sha1,
                read_range_bytes_or_hash=read_range_callback,
                pid=int(target_dir_cid),
                async_=False,
                timeout=30,
            )
        except Exception as e:
            if self._is_cloud_unauthorized_error(e):
                self._trigger_cloud_rapid_auth_cooldown(
                    job_id,
                    counters,
                    auth_cooldown,
                    auth_cooldown_lock,
                    progress_lock,
                    e,
                )
                self._sleep_cloud_rapid_or_cancel(job_id, CLOUD_RAPID_AUTH_COOLDOWN_SECONDS)
                raise RuntimeError("目标账号上传初始化返回 401，按 PyPush 逻辑等待 10 秒后记录失败") from e
            raise
        self._raise_if_cloud_rapid_cancelled(job_id)
        if not isinstance(result, dict) or not result.get("state"):
            error = result.get("error") or result.get("message") if isinstance(result, dict) else "无响应"
            if self._is_cloud_unauthorized_error(RuntimeError(error)):
                self._trigger_cloud_rapid_auth_cooldown(
                    job_id,
                    counters,
                    auth_cooldown,
                    auth_cooldown_lock,
                    progress_lock,
                    RuntimeError(error),
                )
                self._sleep_cloud_rapid_or_cancel(job_id, CLOUD_RAPID_AUTH_COOLDOWN_SECONDS)
                raise RuntimeError("目标账号上传初始化返回 401，按 PyPush 逻辑等待 10 秒后记录失败")
            raise RuntimeError(error or "秒传初始化失败")
        if not result.get("reuse"):
            self._cloud_record_upload_result(cache_db_path, record_key, row, target_dir_cid, "skipped", "秒传未命中，需要真实上传")
            self._record_cloud_rapid_result(
                job_id,
                counters,
                results,
                {
                    "status": "skipped",
                    "type": "file",
                    "name": filename,
                    "path": display_path or filename,
                    "size": size,
                    "pickcode": pickcode,
                    "message": "秒传未命中，需要真实上传",
                    "retrying": retrying,
                },
                progress_lock=progress_lock,
            )
            return

        file_id = self._extract_file_id(result)
        _check_and_move(target_client, file_id, str(target_dir_cid), filename, reused=True)
        self._cloud_record_upload_result(cache_db_path, record_key, row, target_dir_cid, "success", "")
        self._cloud_mark_directory_completion(cache_db_path, task_key, parent_path, str(target_dir_cid))
        self._record_cloud_rapid_result(
            job_id,
            counters,
            results,
            {
                "status": "success",
                "type": "file",
                "name": filename,
                "path": display_path or filename,
                "size": size,
                "pickcode": str(result.get("pickcode") or pickcode),
                "message": "秒传成功",
                "retrying": retrying,
            },
            progress_lock=progress_lock,
        )

    def _ensure_cloud_rapid_target_dir(
        self,
        target_client,
        target_base_cid: str,
        target_base_path: str,
        parent_path: str,
        counters: dict[str, int],
        task_key: str,
        dir_chain_cache: dict,
        dir_lock: threading.Lock,
        job_id: str | None,
    ) -> str:
        self._raise_if_cloud_rapid_cancelled(job_id)
        normalized_parent = self._normalize_cloud_relpath(parent_path)
        if not normalized_parent:
            return str(target_base_cid)
        cache_key = (str(task_key or ""), str(target_base_cid), str(target_base_path or ""), normalized_parent)
        with dir_lock:
            cached = str(dir_chain_cache.get(cache_key, "") or "")
            if cached:
                return cached
            self._raise_if_cloud_rapid_cancelled(job_id)
            self._sync_cloud_rapid_job(
                job_id,
                counters,
                status="running",
                stage="transferring",
                current=normalized_parent,
                message=f"确认目标目录: {normalized_parent}",
            )
            current_parent_cid = str(target_base_cid)
            walked_path = ""
            final_cid = ""
            for segment in normalized_parent.split("/"):
                segment = segment.strip()
                if not segment:
                    continue
                walked_path = self._cloud_join_relpath(walked_path, segment)
                segment_cache_key = (
                    str(task_key or ""),
                    str(target_base_cid),
                    str(target_base_path or ""),
                    walked_path,
                )
                cached_segment = str(dir_chain_cache.get(segment_cache_key, "") or "")
                if cached_segment:
                    current_parent_cid = cached_segment
                    final_cid = cached_segment
                    continue
                next_cid = self._cloud_mkdir_target_dir_web(
                    target_client,
                    current_parent_cid,
                    segment,
                    job_id,
                )
                dir_chain_cache[segment_cache_key] = str(next_cid)
                current_parent_cid = str(next_cid)
                final_cid = str(next_cid)
            if not final_cid:
                final_cid = str(target_base_cid)
            dir_chain_cache[cache_key] = str(final_cid)
            return str(final_cid)

    def _cloud_find_target_child_dir_web(
        self,
        target_client,
        parent_cid: str,
        name: str,
        job_id: str | None,
    ) -> str:
        clean_name = str(name or "").strip()
        if not clean_name:
            return ""
        offset = 0
        while True:
            self._raise_if_cloud_rapid_cancelled(job_id)
            resp = self._fetch_cloud_115_children_page(
                target_client,
                {
                    "cid": int(str(parent_cid or "0")),
                    "limit": CLOUD_RAPID_LIST_LIMIT,
                    "offset": offset,
                    "type": 0,
                    "fc_mix": 0,
                },
                webapi_only=True,
            )
            data = self._extract_115_list_data(resp)
            if not data:
                return ""
            for raw in data:
                entry = self._normalize_cloud_115_entry(raw)
                if entry and entry.get("type") == "dir" and str(entry.get("name") or "") == clean_name:
                    return str(entry.get("cid") or entry.get("id") or "")
            if len(data) < CLOUD_RAPID_LIST_LIMIT:
                return ""
            offset += CLOUD_RAPID_LIST_LIMIT

    def _cloud_mkdir_target_dir_web(
        self,
        target_client,
        parent_cid: str,
        name: str,
        job_id: str | None,
    ) -> str:
        self._raise_if_cloud_rapid_cancelled(job_id)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise RuntimeError("目标目录名为空")
        resp = target_client.fs_mkdir(clean_name, pid=int(str(parent_cid or "0")), async_=False, timeout=20)
        cid = self._extract_cloud_created_dir_cid(resp)
        if cid:
            return cid
        errno = str((resp or {}).get("errno") or (resp or {}).get("errNo") or "")
        message = str((resp or {}).get("error") or (resp or {}).get("message") or "")
        if errno == "20004" or "exist" in message.lower() or "已存在" in message:
            existing_cid = self._cloud_find_target_child_dir_web(target_client, parent_cid, clean_name, job_id)
            if existing_cid:
                return existing_cid
        if not isinstance(resp, dict) or not resp.get("state"):
            raise RuntimeError(message or f"创建目标目录失败: {clean_name}")
        existing_cid = self._cloud_find_target_child_dir_web(target_client, parent_cid, clean_name, job_id)
        if existing_cid:
            return existing_cid
        raise RuntimeError(f"创建目标目录后未获取到 cid: {clean_name}")

    def _extract_cloud_created_dir_cid(self, resp: Any) -> str:
        if not isinstance(resp, dict):
            return ""
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        return str(
            data.get("category_id")
            or data.get("cid")
            or data.get("file_id")
            or resp.get("cid")
            or resp.get("id")
            or resp.get("category_id")
            or ""
        ).strip()

    def _cloud_target_filename_exists(
        self,
        target_client,
        target_dir_cid: str,
        filename: str,
        target_existing_cache: dict[str, set[str]],
        target_existing_lock: threading.Lock,
        job_id: str | None,
    ) -> bool:
        cid = str(target_dir_cid or "").strip()
        clean_name = str(filename or "").strip()
        if not cid or not clean_name:
            return False
        with target_existing_lock:
            names = target_existing_cache.get(cid)
            if names is None:
                names = set()
                offset = 0
                while True:
                    self._raise_if_cloud_rapid_cancelled(job_id)
                    resp = self._fetch_cloud_115_children_page(
                        target_client,
                        {
                            "cid": int(cid),
                            "limit": CLOUD_RAPID_LIST_LIMIT,
                            "offset": offset,
                            "type": 0,
                            "fc_mix": 1,
                        },
                        webapi_only=True,
                    )
                    data = self._extract_115_list_data(resp)
                    if not data:
                        break
                    for raw in data:
                        entry = self._normalize_cloud_115_entry(raw)
                        if entry and entry.get("type") == "file":
                            names.add(str(entry.get("name") or ""))
                    if len(data) < CLOUD_RAPID_LIST_LIMIT:
                        break
                    offset += CLOUD_RAPID_LIST_LIMIT
                target_existing_cache[cid] = names
            return clean_name in names

    def _cloud_note_target_filename(
        self,
        target_dir_cid: str,
        filename: str,
        target_existing_cache: dict[str, set[str]],
        target_existing_lock: threading.Lock,
    ) -> None:
        cid = str(target_dir_cid or "").strip()
        clean_name = str(filename or "").strip()
        if not cid or not clean_name:
            return
        with target_existing_lock:
            names = target_existing_cache.setdefault(cid, set())
            names.add(clean_name)

    def _wait_cloud_rapid_auth_cooldown(
        self,
        job_id: str | None,
        counters: dict[str, int],
        auth_cooldown: dict[str, Any],
        auth_cooldown_lock: threading.Lock,
        progress_lock: threading.Lock | None,
    ) -> None:
        while True:
            self._raise_if_cloud_rapid_cancelled(job_id)
            with auth_cooldown_lock:
                until = float(auth_cooldown.get("until") or 0)
                reason = str(auth_cooldown.get("reason") or "目标账号 401")
            remaining = int(max(0, until - time.monotonic()))
            if remaining <= 0:
                return
            if progress_lock:
                with progress_lock:
                    self._sync_cloud_rapid_job(
                        job_id,
                        counters,
                        status="running",
                        stage="cooldown",
                        current="",
                        message=f"{reason}，冷却 {remaining} 秒后继续",
                    )
            else:
                self._sync_cloud_rapid_job(
                    job_id,
                    counters,
                    status="running",
                    stage="cooldown",
                    current="",
                    message=f"{reason}，冷却 {remaining} 秒后继续",
                )
            self._sleep_cloud_rapid_or_cancel(job_id, min(5, remaining))

    def _trigger_cloud_rapid_auth_cooldown(
        self,
        job_id: str | None,
        counters: dict[str, int],
        auth_cooldown: dict[str, Any],
        auth_cooldown_lock: threading.Lock,
        progress_lock: threading.Lock | None,
        exc: Exception,
    ) -> None:
        reason = "目标账号上传初始化 401，按 PyPush 逻辑等待 10 秒"
        with auth_cooldown_lock:
            now = time.monotonic()
            old_until = float(auth_cooldown.get("until") or 0)
            new_until = max(old_until, now + CLOUD_RAPID_AUTH_COOLDOWN_SECONDS)
            auth_cooldown["until"] = new_until
            auth_cooldown["reason"] = reason
            should_count = new_until > old_until
        if progress_lock:
            with progress_lock:
                if should_count:
                    counters["auth_cooldowns"] = int(counters.get("auth_cooldowns") or 0) + 1
                self._sync_cloud_rapid_job(
                    job_id,
                    counters,
                    status="running",
                    stage="cooldown",
                    current="",
                    message=f"{reason}，冷却 {CLOUD_RAPID_AUTH_COOLDOWN_SECONDS} 秒后继续",
                )
        else:
            if should_count:
                counters["auth_cooldowns"] = int(counters.get("auth_cooldowns") or 0) + 1
            self._sync_cloud_rapid_job(
                job_id,
                counters,
                status="running",
                stage="cooldown",
                current="",
                message=f"{reason}，冷却 {CLOUD_RAPID_AUTH_COOLDOWN_SECONDS} 秒后继续",
            )
        logger.warning(f"[CloudRapid] {reason}: {exc}")

    def _get_cloud_download_url_cached(
        self,
        client,
        cache_db_path: Path,
        pickcode: str,
        info: dict[str, Any],
    ) -> tuple[str, bool]:
        pickcode = str(pickcode or "").strip()
        if not pickcode:
            return "", False
        self._init_cloud_rapid_cache_db(cache_db_path)
        now = int(time.time())
        with self._cloud_sqlite(cache_db_path) as conn:
            cached = conn.execute(
                "SELECT url, timestamp, expires_at FROM url_cache WHERE pickcode = ?",
                (pickcode,),
            ).fetchone()
        if not self._cloud_to_bool(info.get("is_collect")) and cached and self._cloud_cached_url_is_fresh(cached):
            return str(cached["url"] or ""), True

        url = self._get_cloud_download_url(client, pickcode, info)
        if not url:
            return "", False
        self._store_cloud_download_url(cache_db_path, pickcode, url, info, now=now)
        return url, False

    def _refresh_cloud_download_url(
        self,
        client,
        cache_db_path: Path,
        pickcode: str,
        info: dict[str, Any],
    ) -> str:
        pickcode = str(pickcode or "").strip()
        if not pickcode:
            return ""
        url = self._get_cloud_download_url(client, pickcode, info)
        if not url:
            return ""
        self._store_cloud_download_url(cache_db_path, pickcode, url, info)
        return url

    def _store_cloud_download_url(
        self,
        cache_db_path: Path,
        pickcode: str,
        url: str,
        info: dict[str, Any],
        *,
        now: int | None = None,
    ) -> None:
        self._init_cloud_rapid_cache_db(cache_db_path)
        now = int(now or time.time())
        expires_at = self._extract_cloud_url_expires_at(url)
        with self._cloud_sqlite(cache_db_path) as conn:
            existing = conn.execute("SELECT created_at FROM url_cache WHERE pickcode = ?", (pickcode,)).fetchone()
            created_at = int(existing["created_at"]) if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO url_cache(pickcode, url, sha1, filename, timestamp, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pickcode,
                    url,
                    str(info.get("sha1") or "").upper(),
                    str(info.get("name") or ""),
                    now,
                    int(expires_at or 0),
                    created_at,
                    now,
                ),
            )

    def _cloud_cached_url_is_fresh(self, row: sqlite3.Row) -> bool:
        now = int(time.time())
        expires_at = int(row["expires_at"] or 0)
        if expires_at:
            return expires_at - now > CLOUD_RAPID_URL_TTL_BUFFER_SECONDS
        timestamp = int(row["timestamp"] or 0)
        return timestamp > 0 and now - timestamp < max(60, CLOUD_RAPID_FALLBACK_URL_TTL_SECONDS - CLOUD_RAPID_URL_TTL_BUFFER_SECONDS)

    def _extract_cloud_url_expires_at(self, url: str) -> int:
        try:
            query = parse_qs(urlsplit(str(url or "")).query)
            for key in ("t", "expires", "expire", "e"):
                values = query.get(key) or []
                for value in values:
                    if str(value).isdigit():
                        ts = int(value)
                        return ts // 1000 if ts > 10_000_000_000 else ts
        except Exception:
            return 0
        return 0

    def _cloud_rapid_collect_dir(
        self,
        source_client,
        target_client,
        item: dict[str, Any],
        target_parent_cid: str,
        target_parent_path: str,
        file_tasks: list[dict[str, Any]],
        counters: dict[str, int],
        results: list[dict[str, Any]],
        task_key: str,
        dir_chain_cache: dict,
        job_id: str | None = None,
    ) -> None:
        self._raise_if_cloud_rapid_cancelled(job_id)
        if CLOUD_RAPID_MAX_FILES > 0 and counters["total_files"] >= CLOUD_RAPID_MAX_FILES:
            raise RuntimeError(f"本次文件数超过上限 {CLOUD_RAPID_MAX_FILES}")
        dir_name = str(item.get("name") or item.get("cid") or "未命名目录")
        dir_path = "/".join(part for part in [str(target_parent_path or "").strip("/"), dir_name] if part)
        self._sync_cloud_rapid_job(
            job_id,
            counters,
            status="running",
            stage="scanning",
            current=dir_path or dir_name,
            message=f"正在全量同步扫描目录: {dir_name}",
        )
        target_cid, _ = _mkdir_115_dir(
            target_client,
            str(target_parent_cid),
            dir_name,
            task_key=task_key,
            dir_path=dir_path,
        )
        counters["folders"] += 1
        self._sync_cloud_rapid_job(job_id, counters, message=f"已确认目标目录: {dir_path or dir_name}")
        entries = self._list_cloud_115_tree_entries(source_client, str(item.get("cid") or item.get("id")))
        self._raise_if_cloud_rapid_cancelled(job_id)
        target_dir_map: dict[str, str] = {"": str(target_cid)}
        dirs = sorted(
            [entry for entry in entries if entry.get("type") == "dir"],
            key=lambda entry: str(entry.get("relpath") or entry.get("path") or "").count("/"),
        )
        files = [entry for entry in entries if entry.get("type") == "file"]
        for child in dirs:
            self._raise_if_cloud_rapid_cancelled(job_id)
            try:
                rel_dir = str(child.get("relpath") or child.get("path") or child.get("name") or "").strip("/")
                if not rel_dir:
                    continue
                parent_rel = posixpath.dirname(rel_dir).strip("/")
                child_name = posixpath.basename(rel_dir) or str(child.get("name") or "")
                parent_target_cid = target_dir_map.get(parent_rel, str(target_cid))
                child_target_path = "/".join(part for part in [dir_path, rel_dir] if part)
                child_target_cid, _ = _mkdir_115_dir(
                    target_client,
                    parent_target_cid,
                    child_name,
                    task_key=task_key,
                    dir_path=child_target_path,
                )
                target_dir_map[rel_dir] = str(child_target_cid)
                counters["folders"] += 1
                self._sync_cloud_rapid_job(
                    job_id,
                    counters,
                    status="running",
                    stage="scanning",
                    current=child_target_path,
                    message=f"已确认目标目录: {child_target_path}",
                )
            except Exception as e:
                self._record_cloud_rapid_result(
                    job_id,
                    counters,
                    results,
                    {
                        "status": "failed",
                        "type": child.get("type", "file"),
                        "name": child.get("name", ""),
                        "path": "/".join(part for part in [dir_path, str(child.get("name") or "")] if part),
                        "message": str(e),
                    },
                    count_processed=False,
                )
        for child in files:
            self._raise_if_cloud_rapid_cancelled(job_id)
            rel_file = str(child.get("relpath") or child.get("path") or child.get("name") or "").strip("/")
            parent_rel = posixpath.dirname(rel_file).strip("/")
            relative_name = "/".join(part for part in [dir_path, rel_file] if part)
            self._cloud_rapid_collect_file(
                child,
                target_dir_map.get(parent_rel, str(target_cid)),
                relative_name,
                file_tasks,
                counters,
                job_id=job_id,
            )

    def _cloud_rapid_collect_file(
        self,
        item: dict[str, Any],
        target_cid: str,
        display_path: str,
        file_tasks: list[dict[str, Any]],
        counters: dict[str, int],
        job_id: str | None = None,
    ) -> None:
        self._raise_if_cloud_rapid_cancelled(job_id)
        if CLOUD_RAPID_MAX_FILES > 0 and counters["total_files"] >= CLOUD_RAPID_MAX_FILES:
            raise RuntimeError(f"本次文件数超过上限 {CLOUD_RAPID_MAX_FILES}")
        counters["total_files"] += 1
        file_tasks.append({
            "item": copy.deepcopy(item),
            "target_cid": str(target_cid),
            "display_path": display_path or str(item.get("name") or ""),
        })
        self._sync_cloud_rapid_job(
            job_id,
            counters,
            status="running",
            stage="scanning",
            current=display_path or str(item.get("name") or ""),
            message=f"已扫描文件: {item.get('name') or display_path}",
        )

    def _execute_cloud_rapid_file_task(
        self,
        source_client,
        target_client,
        file_task: dict[str, Any],
        counters: dict[str, int],
        results: list[dict[str, Any]],
        job_id: str | None,
        progress_lock: threading.Lock,
    ) -> None:
        item = dict(file_task.get("item") or {})
        display_path = str(file_task.get("display_path") or item.get("name") or "")
        try:
            self._raise_if_cloud_rapid_cancelled(job_id)
            self._cloud_rapid_transfer_file(
                source_client,
                target_client,
                item,
                str(file_task.get("target_cid") or ""),
                display_path,
                counters,
                results,
                job_id=job_id,
                progress_lock=progress_lock,
            )
        except CloudRapidCancelled:
            raise
        except Exception as e:
            self._record_cloud_rapid_result(
                job_id,
                counters,
                results,
                {
                    "status": "failed",
                    "type": item.get("type", "file"),
                    "name": item.get("name", ""),
                    "path": display_path or item.get("path", item.get("name", "")),
                    "message": str(e),
                },
                progress_lock=progress_lock,
            )

    def _cloud_rapid_transfer_file(
        self,
        source_client,
        target_client,
        item: dict[str, Any],
        target_cid: str,
        display_path: str,
        counters: dict[str, int],
        results: list[dict[str, Any]],
        job_id: str | None = None,
        progress_lock: threading.Lock | None = None,
    ) -> None:
        self._raise_if_cloud_rapid_cancelled(job_id)
        self._sync_cloud_rapid_job(
            job_id,
            counters,
            status="running",
            stage="transferring",
            current=display_path or str(item.get("name") or ""),
            message=f"准备秒传: {item.get('name') or display_path}",
        )
        info = self._resolve_cloud_source_file_info(source_client, item)
        filename = str(info.get("name") or item.get("name") or "video.mkv")
        pickcode = str(info.get("pickcode") or "")
        if not pickcode:
            raise RuntimeError(f"缺少 pickcode: {filename}")
        if not info.get("sha1") or not int(info.get("size") or 0):
            raise RuntimeError(f"缺少 SHA1 或大小: {filename}")
        self._raise_if_cloud_rapid_cancelled(job_id)
        download_url = self._get_cloud_download_url(source_client, pickcode, info)
        if not download_url:
            raise RuntimeError(f"无法获取来源直链: {filename}")

        def read_range_callback(sign_check: str) -> bytes:
            self._raise_if_cloud_rapid_cancelled(job_id)
            return self._read_cloud_range_bytes(download_url, sign_check, job_id=job_id)

        try:
            self._raise_if_cloud_rapid_cancelled(job_id)
            result = target_client.upload_file_init(
                filename=filename,
                filesize=int(info["size"]),
                filesha1=str(info["sha1"]).upper(),
                read_range_bytes_or_hash=read_range_callback,
                pid=int(target_cid),
                async_=False,
                timeout=30,
            )
        except Exception as e:
            if self._is_cloud_unauthorized_error(e):
                self._sleep_cloud_rapid_or_cancel(job_id, 10)
            raise
        self._raise_if_cloud_rapid_cancelled(job_id)
        if not isinstance(result, dict) or not result.get("state"):
            error = result.get("error") or result.get("message") if isinstance(result, dict) else "无响应"
            raise RuntimeError(error or "秒传初始化失败")
        if not result.get("reuse"):
            self._record_cloud_rapid_result(
                job_id,
                counters,
                results,
                {
                    "status": "skipped",
                    "type": "file",
                    "name": filename,
                    "path": display_path or filename,
                    "size": int(info["size"]),
                    "message": "秒传未命中，需要真实上传",
                },
                progress_lock=progress_lock,
            )
            return
        self._record_cloud_rapid_result(job_id, counters, results, {
            "status": "success",
            "type": "file",
            "name": filename,
            "path": display_path or filename,
            "size": int(info["size"]),
            "pickcode": str(result.get("pickcode") or ""),
            "message": "秒传成功",
        }, progress_lock=progress_lock)

    def _resolve_cloud_source_file_info(self, source_client, item: dict[str, Any]) -> dict[str, Any]:
        info = {
            "name": str(item.get("name") or ""),
            "file_id": str(item.get("file_id") or item.get("id") or ""),
            "pickcode": str(item.get("pickcode") or "").strip(),
            "sha1": str(item.get("sha1") or "").strip().upper(),
            "size": int(item.get("size") or 0),
            "is_collect": self._cloud_to_bool(item.get("is_collect")),
        }
        if info["pickcode"] and (not info["file_id"] or not info["sha1"] or not info["size"]):
            try:
                info["file_id"] = str(to_id(info["pickcode"]))
            except Exception:
                pass
        if info["file_id"] and (not info["pickcode"] or not info["sha1"] or not info["size"] or not info["name"]):
            attr = get_attr(source_client, int(info["file_id"])) or {}
            info["name"] = info["name"] or str(attr.get("name") or attr.get("file_name") or "")
            info["pickcode"] = info["pickcode"] or str(attr.get("pickcode") or attr.get("pick_code") or "")
            info["sha1"] = info["sha1"] or str(attr.get("sha1") or "").strip().upper()
            info["size"] = info["size"] or int(attr.get("size") or 0)
            info["is_collect"] = info["is_collect"] or self._cloud_to_bool(attr.get("is_collect"))
        return info

    def _get_cloud_download_url(self, client, pickcode: str, info: dict[str, Any] | None = None) -> str:
        download_app = "web" if info and self._cloud_to_bool(info.get("is_collect")) else "android"
        url_obj = client.download_url(
            str(pickcode or "").strip(),
            app=download_app,
            async_=False,
            timeout=15,
        )
        url = getattr(url_obj, "url", None)
        if url:
            return str(url).strip()
        return self._extract_download_url_value(url_obj)

    def _extract_download_url_value(self, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            return str(value.get("url") or "").strip()
        return ""

    def _read_cloud_range_bytes(self, download_url: str, sign_check: str, job_id: str | None = None) -> bytes:
        try:
            self._raise_if_cloud_rapid_cancelled(job_id)
            with self._cloud_range_semaphore:
                self._raise_if_cloud_rapid_cancelled(job_id)
                resp = httpx.get(
                    download_url,
                    headers={"Range": f"bytes={sign_check}", "User-Agent": ""},
                    timeout=15,
                    verify=False,
                    follow_redirects=True,
                )
            if resp.status_code != 206:
                raise RuntimeError(f"fetch_range returned HTTP {resp.status_code}")
            self._raise_if_cloud_rapid_cancelled(job_id)
            return resp.content
        except Exception as e:
            logger.warning(f"[CloudRapid] 范围校验失败: {e}")
            raise

    def _read_cloud_range_sha1(self, download_url: str, sign_check: str, job_id: str | None = None) -> str:
        return hashlib.sha1(self._read_cloud_range_bytes(download_url, sign_check, job_id=job_id)).hexdigest().upper()

    def _is_cloud_unauthorized_error(self, exc: Exception) -> bool:
        text = str(exc)
        return "401" in text or "Unauthorized" in text

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._tasks = self._read_json(CONFIG_FILE, [])
        if not isinstance(self._tasks, list):
            self._tasks = []
        state = self._read_json(STATE_FILE, {"tasks": {}})
        if not isinstance(state, dict):
            state = {"tasks": {}}
        state.setdefault("tasks", {})
        self._state = state
        self._rebuild_known_keys_locked()
        migrated_count = 0
        for task in self._tasks:
            if not task.get("upload_defaults_v2_migrated"):
                task["include_existing_on_start"] = True
                task["delete_local_after_success"] = True
                task["upload_defaults_v2_migrated"] = True
                migrated_count += 1
            task_id = str(task.get("id") or "")
            if task_id:
                self._ensure_task_state_locked(task_id)
        if migrated_count:
            self._save_tasks_locked()
            logger.info(f"[Drive115Upload] 已迁移上传任务默认开关为开启: {migrated_count} 个")
        self._loaded = True

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return copy.deepcopy(default)
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[Drive115Upload] 读取配置失败 {path}: {e}")
            return copy.deepcopy(default)

    def _save_tasks_locked(self) -> None:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(self._tasks, f, ensure_ascii=False, indent=2)

    def _save_state_locked(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

    def _normalize_task(self, payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("任务名称不能为空")
        local_folder = os.path.abspath(os.path.expanduser(str(payload.get("local_folder") or "").strip()))
        if not local_folder or not os.path.isdir(local_folder):
            raise ValueError("本地监听目录不存在")
        target_cid = str(payload.get("target_cid") or "").strip()
        if not target_cid.isdigit() or target_cid == "0":
            raise ValueError("请选择非根目录的 115 目标文件夹")
        concurrency = int(payload.get("concurrency") or DEFAULT_UPLOAD_WORKERS)
        concurrency = max(1, min(MAX_UPLOAD_WORKERS, concurrency))
        base = dict(existing or {})
        base.update({
            "name": name,
            "enabled": bool(payload.get("enabled", True)),
            "drive_index": max(0, int(payload.get("drive_index") or 0)),
            "local_folder": local_folder,
            "target_cid": target_cid,
            "target_name": str(payload.get("target_name") or "").strip(),
            "target_path": str(payload.get("target_path") or "").strip(),
            "watch_mode": "realtime",
            "include_existing_on_start": bool(payload.get("include_existing_on_start", True)),
            "delete_local_after_success": bool(payload.get("delete_local_after_success", True)),
            "upload_defaults_v2_migrated": True,
            "concurrency": concurrency,
        })
        return base

    def _find_task_locked(self, task_id: str) -> tuple[int, dict[str, Any]]:
        for idx, task in enumerate(self._tasks):
            if str(task.get("id") or "") == str(task_id):
                return idx, task
        raise KeyError("任务不存在")

    def _get_task_locked(self, task_id: str) -> dict[str, Any] | None:
        for task in self._tasks:
            if str(task.get("id") or "") == str(task_id):
                return task
        return None

    def _get_task_copy(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._load_locked()
            task = self._get_task_locked(task_id)
            return copy.deepcopy(task) if task else None

    def _ensure_task_state_locked(self, task_id: str) -> dict[str, Any]:
        tasks_state = self._state.setdefault("tasks", {})
        state = tasks_state.setdefault(task_id, {})
        state.setdefault("running", False)
        state.setdefault("queue_size", 0)
        state.setdefault("active", [])
        state.setdefault("recent", [])
        state.setdefault("failed", [])
        state.setdefault("message", "")
        return state

    def _clear_task_runtime_keys_locked(self, task_id: str) -> None:
        prefix = f"{task_id}:"
        self._seen_keys = {key for key in self._seen_keys if not key.startswith(prefix)}
        self._queued_keys = {key for key in self._queued_keys if not key.startswith(prefix)}
        self._active_keys = {key for key in self._active_keys if not key.startswith(prefix)}

    def _rebuild_known_keys_locked(self) -> None:
        self._completed_keys.clear()
        self._failed_keys.clear()
        for task_state in self._state.get("tasks", {}).values():
            for record in task_state.get("recent", []):
                key = str(record.get("key") or "")
                if key:
                    self._completed_keys.add(key)
            for record in task_state.get("failed", []):
                key = str(record.get("key") or "")
                if key:
                    self._failed_keys.add(key)

    def _sync_watchers_locked(self) -> None:
        active_task_ids = {str(task.get("id") or "") for task in self._tasks if task.get("enabled", True)}
        for task_id in list(self._watchers.keys()):
            if task_id not in active_task_ids:
                self._stop_watcher_locked(task_id)
        for task in self._tasks:
            if task.get("enabled", True):
                self._start_watcher_locked(task)

    def _start_watcher_locked(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("id") or "")
        if not task_id:
            return
        if self._is_watcher_running_locked(task_id):
            return
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._watch_task_loop,
            args=(task_id, stop_event),
            name=f"drive115-upload-watch-{task_id[-8:]}",
            daemon=True,
        )
        self._watcher_stops[task_id] = stop_event
        self._watchers[task_id] = thread
        state = self._ensure_task_state_locked(task_id)
        state["running"] = True
        state["message"] = "监听中"
        thread.start()

    def _restart_watcher_locked(self, task_id: str, task: dict[str, Any]) -> None:
        self._stop_watcher_locked(task_id)
        if task.get("enabled", True):
            self._start_watcher_locked(task)

    def _stop_watcher_locked(self, task_id: str) -> None:
        event = self._watcher_stops.pop(task_id, None)
        if event:
            event.set()
        thread = self._watchers.pop(task_id, None)
        if thread and thread.is_alive():
            thread.join(timeout=0.5)
        state = self._ensure_task_state_locked(task_id)
        state["running"] = False
        state["message"] = "已停止"

    def _is_watcher_running_locked(self, task_id: str) -> bool:
        thread = self._watchers.get(task_id)
        return bool(thread and thread.is_alive())

    def _ensure_workers_locked(self) -> None:
        desired = DEFAULT_UPLOAD_WORKERS
        for task in self._tasks:
            if task.get("enabled", True):
                desired = max(desired, int(task.get("concurrency") or 1))
        desired = max(1, min(MAX_UPLOAD_WORKERS, desired))
        self._workers = [thread for thread in self._workers if thread.is_alive()]
        while len(self._workers) < desired:
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"drive115-upload-worker-{len(self._workers) + 1}",
                daemon=True,
            )
            self._workers.append(thread)
            thread.start()

    def _watch_task_loop(self, task_id: str, stop_event: threading.Event) -> None:
        task = self._get_task_copy(task_id) or {}
        logger.trace(
            f"[Drive115Upload] 监听任务启动: {task_id} | 本地={task.get('local_folder') or '-'} | "
            f"115={task.get('target_path') or task.get('target_name') or task.get('target_cid') or '-'} | 递归扫描=开启"
        )
        first_scan = True
        while not self._stop_event.is_set() and not stop_event.is_set():
            task = self._get_task_copy(task_id)
            if not task or not task.get("enabled", True):
                break
            try:
                if first_scan and not task.get("include_existing_on_start", False):
                    self._remember_current_files(task)
                else:
                    self._scan_task_files(task, force=False)
                with self._lock:
                    state = self._ensure_task_state_locked(task_id)
                    state["running"] = True
                    state["message"] = "监听中"
            except Exception as e:
                logger.warning(f"[Drive115Upload] 扫描监听目录失败 {task.get('local_folder')}: {e}")
                with self._lock:
                    state = self._ensure_task_state_locked(task_id)
                    state["message"] = f"扫描失败: {e}"
            first_scan = False
            stop_event.wait(WATCH_SCAN_INTERVAL_SECONDS)
        with self._lock:
            state = self._ensure_task_state_locked(task_id)
            state["running"] = False
            if not self._started:
                state["message"] = "已停止"
        logger.info(f"[Drive115Upload] 监听任务停止: {task_id}")

    def _remember_current_files(self, task: dict[str, Any]) -> None:
        remembered = 0
        for path in self._iter_candidate_files(task):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            key = self._make_key(str(task.get("id")), path, stat.st_size, int(stat.st_mtime_ns))
            with self._lock:
                self._seen_keys.add(key)
            remembered += 1
        if remembered:
            logger.debug(f"[Drive115Upload] 已忽略启动前已有文件 {remembered} 个: {task.get('name')}")

    def _scan_task_files(self, task: dict[str, Any], force: bool) -> int:
        queued = 0
        scanned = 0
        for path in self._iter_candidate_files(task):
            scanned += 1
            info = self._get_stable_file_info(path)
            if not info:
                continue
            if self._enqueue_file(task, path, info["size"], info["mtime_ns"], force=force, attempts=1, source="scan"):
                queued += 1
        if scanned or queued:
            logger.info(f"[Drive115Upload] 扫描完成: {task.get('name') or task.get('id')} | 发现={scanned} | 入队={queued}")
        return queued

    def _iter_candidate_files(self, task: dict[str, Any]):
        folder = str(task.get("local_folder") or "")
        if not os.path.isdir(folder):
            return
        for root, dirs, files in os.walk(folder, topdown=True, followlinks=False):
            dirs[:] = [name for name in dirs if self._is_candidate_name(name)]
            for name in files:
                if not self._is_candidate_name(name):
                    continue
                path = os.path.join(root, name)
                try:
                    if os.path.isfile(path) and not os.path.islink(path):
                        yield path
                except OSError:
                    continue

    def _is_candidate_name(self, name: str) -> bool:
        if name.startswith("."):
            return False
        lower = name.lower()
        return lower not in {"thumbs.db", ".ds_store"} and not lower.endswith(TEMP_SUFFIXES)

    def _get_stable_file_info(self, path: str) -> dict[str, Any] | None:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        size = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)
        now = time.time()
        if now - float(stat.st_mtime) >= FILE_STABLE_SECONDS:
            return {"size": size, "mtime_ns": mtime_ns}
        with self._lock:
            previous = self._stability.get(path)
            if previous and previous[0] == size and previous[1] == mtime_ns:
                if now - previous[2] >= FILE_STABLE_SECONDS:
                    return {"size": size, "mtime_ns": mtime_ns}
            else:
                self._stability[path] = (size, mtime_ns, now)
        return None

    def _enqueue_file(
        self,
        task: dict[str, Any],
        path: str,
        size: int,
        mtime_ns: int,
        force: bool = False,
        attempts: int = 1,
        source: str = "watch",
    ) -> bool:
        task_id = str(task.get("id") or "")
        local_folder = os.path.abspath(str(task.get("local_folder") or ""))
        path_abs = os.path.abspath(path)
        relative_path = self._relative_upload_path(local_folder, path_abs)
        relative_dir = os.path.dirname(relative_path).replace(os.sep, "/").strip("/")
        key = self._make_key(task_id, path_abs, size, mtime_ns)
        with self._lock:
            if key in self._queued_keys or key in self._active_keys or key in self._completed_keys:
                return False
            if not force and (key in self._seen_keys or key in self._failed_keys):
                return False
            job = {
                "job_id": f"upload_{int(time.time())}_{uuid.uuid4().hex[:8]}",
                "task_id": task_id,
                "task_name": task.get("name", ""),
                "path": path_abs,
                "filename": os.path.basename(path_abs),
                "local_folder": local_folder,
                "relative_path": relative_path,
                "relative_dir": relative_dir,
                "size": int(size),
                "mtime_ns": int(mtime_ns),
                "key": key,
                "drive_index": int(task.get("drive_index") or 0),
                "target_cid": str(task.get("target_cid") or ""),
                "target_name": str(task.get("target_name") or ""),
                "target_path": str(task.get("target_path") or ""),
                "delete_local_after_success": bool(task.get("delete_local_after_success", False)),
                "task_updated_at": int(task.get("updated_at") or 0),
                "attempts": int(attempts or 1),
                "source": source,
                "queued_at": int(time.time()),
            }
            try:
                self._queue.put_nowait(job)
            except queue.Full:
                state = self._ensure_task_state_locked(task_id)
                state["message"] = "上传队列已满"
                return False
            self._queued_keys.add(key)
            self._seen_keys.add(key)
            self._queued_count[task_id] = self._queued_count.get(task_id, 0) + 1
            state = self._ensure_task_state_locked(task_id)
            state["queue_size"] = self._queued_count[task_id]
            state["message"] = "已加入上传队列"
            logger.info(f"[Drive115Upload] 已加入上传队列: {relative_path} -> {task.get('target_path') or task.get('target_name') or task.get('target_cid')}")
            return True

    def _relative_upload_path(self, local_folder: str, path: str) -> str:
        rel_path = os.path.relpath(path, local_folder)
        if rel_path == "." or rel_path.startswith(".." + os.sep) or rel_path == "..":
            raise ValueError("文件不在监听目录内")
        return rel_path.replace(os.sep, "/")

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                claimed = self._claim_job(job)
                if claimed is None:
                    continue
                if not claimed:
                    if not self._stop_event.is_set():
                        time.sleep(0.5)
                        self._queue.put(job)
                    continue
                try:
                    self._process_upload(job)
                except Exception as e:
                    logger.exception(f"[Drive115Upload] 上传失败 {job.get('path')}: {e}")
                    self._mark_failed(job, str(e))
                finally:
                    self._finish_active_job(job)
            finally:
                self._queue.task_done()

    def _claim_job(self, job: dict[str, Any]) -> bool:
        task_id = str(job.get("task_id") or "")
        key = str(job.get("key") or "")
        with self._lock:
            task = self._get_task_locked(task_id)
            if not task:
                self._queued_keys.discard(key)
                self._queued_count[task_id] = max(0, self._queued_count.get(task_id, 0) - 1)
                return None
            if not task.get("enabled", True) or int(task.get("updated_at") or 0) != int(job.get("task_updated_at") or 0):
                self._queued_keys.discard(key)
                self._queued_count[task_id] = max(0, self._queued_count.get(task_id, 0) - 1)
                return None
            if self._active_count.get(task_id, 0) >= int(task.get("concurrency") or 1):
                return False
            self._queued_keys.discard(key)
            self._queued_count[task_id] = max(0, self._queued_count.get(task_id, 0) - 1)
            self._active_keys.add(key)
            self._active_count[task_id] = self._active_count.get(task_id, 0) + 1
            active = self._active_record(job, "checking", 5, "准备秒传检测")
            active["started_at"] = int(time.time())
            self._active_jobs[str(job.get("job_id"))] = active
            state = self._ensure_task_state_locked(task_id)
            state["queue_size"] = self._queued_count[task_id]
            state["message"] = "正在上传"
            return True

    def _finish_active_job(self, job: dict[str, Any]) -> None:
        task_id = str(job.get("task_id") or "")
        key = str(job.get("key") or "")
        with self._lock:
            self._active_jobs.pop(str(job.get("job_id")), None)
            self._active_keys.discard(key)
            self._active_count[task_id] = max(0, self._active_count.get(task_id, 0) - 1)

    def _process_upload(self, job: dict[str, Any]) -> None:
        from p115client.tool.upload import P115MultipartUpload

        path = str(job.get("path") or "")
        filename = str(job.get("filename") or os.path.basename(path))
        target_cid = str(job.get("target_cid") or "")
        if not os.path.isfile(path):
            raise FileNotFoundError("本地文件不存在")
        stat = os.stat(path)
        if int(stat.st_size) != int(job.get("size") or 0) or int(stat.st_mtime_ns) != int(job.get("mtime_ns") or 0):
            raise RuntimeError("文件上传前发生变化，请等待下一轮稳定扫描")
        client = self.get_client(int(job.get("drive_index") or 0))
        upload_cid = self._resolve_upload_target_cid(client, job)
        job["upload_target_cid"] = str(upload_cid)
        self._update_active(job, "checking", 10, "正在尝试秒传")
        try:
            result = P115MultipartUpload.from_path(
                path,
                pid=int(upload_cid),
                filename=filename,
                user_id=client.user_id,
                user_key=client.user_key,
                async_=False,
            )
        except Exception as e:
            if not self._should_fallback_without_rapid(e):
                raise
            logger.warning(
                f"[Drive115Upload] 秒传初始化异常，改用普通上传: "
                f"{job.get('relative_path') or filename}: {e}",
                exc_info=True,
            )
            self._upload_without_rapid(client, job, upload_cid, filename, e)
            return
        if isinstance(result, dict):
            if result.get("state") is False:
                error = result.get("error") or result.get("message") or "秒传初始化失败"
                rapid_error = RuntimeError(error)
                if self._should_fallback_without_rapid(rapid_error):
                    logger.warning(
                        f"[Drive115Upload] 秒传初始化返回异常，改用普通上传: "
                        f"{job.get('relative_path') or filename}: {error}"
                    )
                    self._upload_without_rapid(client, job, upload_cid, filename, rapid_error)
                    return
                raise rapid_error
            file_id = self._extract_file_id(result)
            _check_and_move(client, file_id, upload_cid, filename, reused=True)
            self._mark_success(job, method="rapid", message="秒传成功")
            logger.info(f"[Drive115Upload] 秒传成功: {job.get('relative_path') or filename} -> cid={upload_cid}")
            return

        uploader = result
        uploaded = 0
        size = int(job.get("size") or 0)

        def reporthook(delta: int) -> None:
            nonlocal uploaded
            uploaded += int(delta or 0)
            progress = min(99, int(uploaded / size * 100)) if size > 0 else None
            self._update_active(job, "uploading", progress, "正在真实上传", uploaded=uploaded)

        self._update_active(job, "uploading", 15, "秒传未命中，开始真实上传", uploaded=0)
        for _ in uploader.iter_upload(reporthook=reporthook, async_=False):
            pass
        self._update_active(job, "uploading", 99, "正在完成上传", uploaded=uploaded)
        complete_result = uploader.complete(async_=False)
        if not complete_result or not complete_result.get("state"):
            if isinstance(complete_result, dict):
                error = complete_result.get("error") or complete_result.get("message")
            else:
                error = "无响应"
            raise RuntimeError(error or "上传完成接口返回失败")
        file_id = self._extract_file_id(complete_result)
        _check_and_move(client, file_id, upload_cid, filename, reused=False)
        self._mark_success(job, method="multipart", message="真实上传成功")
        logger.info(f"[Drive115Upload] 真实上传成功: {job.get('relative_path') or filename} -> cid={upload_cid}")

    def _should_fallback_without_rapid(self, error: Exception) -> bool:
        message = str(error or "").lower()
        return (
            "index out of bounds" in message
            or "out of bounds on dimension" in message
            or "sign_check" in message
        )

    def _upload_without_rapid(self, client, job: dict[str, Any], upload_cid: str, filename: str, rapid_error: Exception) -> None:
        path = str(job.get("path") or "")
        self._update_active(job, "uploading", 15, "秒传校验异常，改用普通上传", uploaded=0)
        result = client.upload_file_sample(
            path,
            pid=int(upload_cid),
            filename=filename,
            async_=False,
        )
        if not isinstance(result, dict) or result.get("state") is False:
            error = "普通上传无响应"
            if isinstance(result, dict):
                error = result.get("error") or result.get("message") or error
            raise RuntimeError(f"秒传校验异常且普通上传失败: {error}") from rapid_error
        self._update_active(job, "uploading", 99, "正在完成上传", uploaded=int(job.get("size") or 0))
        file_id = self._extract_file_id(result)
        _check_and_move(client, file_id, upload_cid, filename, reused=False)
        self._mark_success(job, method="multipart", message="秒传校验异常，已改用普通上传成功")
        logger.info(f"[Drive115Upload] 普通上传兜底成功: {job.get('relative_path') or filename} -> cid={upload_cid}")

    def _resolve_upload_target_cid(self, client, job: dict[str, Any]) -> str:
        target_cid = str(job.get("target_cid") or "")
        relative_dir = str(job.get("relative_dir") or "").strip("/")
        if not relative_dir:
            return target_cid
        task_key = f"drive115_upload:{job.get('task_id') or ''}:{job.get('drive_index') or 0}:{target_cid}"
        upload_cid = _ensure_115_dir_chain_cached(
            client,
            target_cid,
            relative_dir,
            self._dir_chain_cache,
            task_key=task_key,
            base_path=str(job.get("target_path") or "").strip("/"),
        )
        logger.info(f"[Drive115Upload] 远端目录已确认: {relative_dir} -> cid={upload_cid}")
        return str(upload_cid)

    def _update_active(self, job: dict[str, Any], stage: str, progress: int | None, message: str, uploaded: int | None = None) -> None:
        job_id = str(job.get("job_id") or "")
        with self._lock:
            active = self._active_jobs.get(job_id)
            if not active:
                return
            active["stage"] = stage
            active["progress"] = progress
            active["message"] = message
            active["updated_at"] = int(time.time())
            if uploaded is not None:
                active["uploaded"] = int(uploaded)

    def _mark_success(self, job: dict[str, Any], method: str, message: str) -> None:
        deleted_message = ""
        if job.get("delete_local_after_success"):
            path = str(job.get("path") or "")
            try:
                os.remove(path)
                removed_dirs = self._cleanup_empty_parent_dirs(path, str(job.get("local_folder") or ""))
                deleted_message = "，已删除本地文件"
                if removed_dirs:
                    deleted_message += f"并清理 {removed_dirs} 个空目录"
                logger.info(f"[Drive115Upload] 已删除本地文件: {path}")
            except Exception as e:
                deleted_message = f"，本地删除失败: {e}"
                logger.warning(f"[Drive115Upload] 本地文件删除失败: {path}: {e}")
        record = self._history_record(job)
        record.update({
            "status": "success",
            "method": method,
            "stage": "success",
            "progress": 100,
            "message": f"{message}{deleted_message}",
            "finished_at": int(time.time()),
        })
        key = str(job.get("key") or "")
        with self._lock:
            state = self._ensure_task_state_locked(str(job.get("task_id") or ""))
            state.setdefault("recent", []).insert(0, record)
            del state["recent"][MAX_HISTORY:]
            state["message"] = record["message"]
            if key:
                self._completed_keys.add(key)
                self._failed_keys.discard(key)
            self._save_state_locked()

    def _cleanup_empty_parent_dirs(self, path: str, root: str) -> int:
        root_abs = os.path.abspath(root)
        current = os.path.abspath(os.path.dirname(path))
        removed = 0
        while current != root_abs and current.startswith(root_abs + os.sep):
            try:
                os.rmdir(current)
                removed += 1
                logger.info(f"[Drive115Upload] 已清理本地空目录: {current}")
            except OSError:
                break
            current = os.path.dirname(current)
        return removed

    def _mark_failed(self, job: dict[str, Any], error: str) -> None:
        record = self._history_record(job)
        record.update({
            "status": "failed",
            "stage": "failed",
            "progress": 0,
            "error": str(error or "未知错误"),
            "message": str(error or "未知错误"),
            "failed_at": int(time.time()),
        })
        key = str(job.get("key") or "")
        with self._lock:
            state = self._ensure_task_state_locked(str(job.get("task_id") or ""))
            state.setdefault("failed", []).insert(0, record)
            del state["failed"][MAX_HISTORY:]
            state["message"] = f"上传失败: {record['error']}"
            if key:
                self._failed_keys.add(key)
            self._save_state_locked()

    def _active_record(self, job: dict[str, Any], stage: str, progress: int | None, message: str) -> dict[str, Any]:
        record = self._history_record(job)
        record.update({
            "status": "active",
            "stage": stage,
            "progress": progress,
            "message": message,
            "uploaded": 0,
            "updated_at": int(time.time()),
        })
        return record

    def _history_record(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(job.get("job_id") or ""),
            "task_id": str(job.get("task_id") or ""),
            "path": str(job.get("path") or ""),
            "filename": str(job.get("filename") or ""),
            "relative_path": str(job.get("relative_path") or ""),
            "relative_dir": str(job.get("relative_dir") or ""),
            "size": int(job.get("size") or 0),
            "key": str(job.get("key") or ""),
            "target_cid": str(job.get("target_cid") or ""),
            "upload_target_cid": str(job.get("upload_target_cid") or job.get("target_cid") or ""),
            "target_name": str(job.get("target_name") or ""),
            "target_path": str(job.get("target_path") or ""),
            "attempts": int(job.get("attempts") or 1),
            "queued_at": int(job.get("queued_at") or time.time()),
        }

    def _extract_file_id(self, result: dict[str, Any]) -> Any:
        data = result.get("data", {})
        if isinstance(data, dict):
            return data.get("file_id") or data.get("id") or data.get("fid")
        return result.get("file_id") or result.get("id") or result.get("fid")

    def _make_key(self, task_id: str, path: str, size: int, mtime_ns: int) -> str:
        return f"{task_id}:{os.path.abspath(path)}:{int(size)}:{int(mtime_ns)}"

    def _get_cookie(self, drive_index: int) -> str:
        if not CONFIG_302_FILE.exists():
            raise RuntimeError("302 配置不存在")
        try:
            with CONFIG_302_FILE.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            raise RuntimeError(f"读取 302 配置失败: {e}")
        drives = cfg.get("drives", []) if isinstance(cfg, dict) else []
        drive_cfg = None
        if isinstance(drives, list) and drives:
            idx = max(0, min(int(drive_index or 0), len(drives) - 1))
            drive_cfg = drives[idx]
        elif isinstance(cfg, dict):
            drive_cfg = cfg.get("drive", {})
        if not isinstance(drive_cfg, dict) or not drive_cfg:
            raise RuntimeError("未配置 115 账号")
        cookie = str(drive_cfg.get("cookie", "") or "").strip()
        if not cookie:
            raise RuntimeError("Cookie 未配置")
        return cookie


drive115_upload_service = Drive115UploadService()
