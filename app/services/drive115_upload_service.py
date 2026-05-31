from __future__ import annotations

import copy
import hashlib
import json
import os
import posixpath
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
from p115client import P115Client
from p115client.tool.attr import get_attr
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
CLOUD_RAPID_MAX_FILES = 5000
CLOUD_RAPID_RESULT_LIMIT = 200
CLOUD_RAPID_JOB_KEEP_SECONDS = 24 * 60 * 60
CLOUD_RAPID_JOB_HISTORY_LIMIT = 20
CLOUD_RAPID_DEFAULT_CONCURRENCY = 1
CLOUD_RAPID_MAX_CONCURRENCY = 10
CLOUD_RAPID_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36"
TEMP_SUFFIXES = (
    ".crdownload",
    ".part",
    ".tmp",
    ".!qb",
    ".!ut",
    ".download",
)


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
            "progress": 0,
            "current": "",
            "results": [],
            "truncated": False,
            "started_at": now,
            "updated_at": now,
            "finished_at": 0,
        }
        with self._lock:
            self._prune_cloud_rapid_jobs_locked()
            self._cloud_rapid_jobs[job_id] = job
        thread = threading.Thread(
            target=self._cloud_rapid_transfer_worker,
            args=(job_id, task),
            name=f"drive115-cloud-rapid-{job_id[-8:]}",
            daemon=True,
        )
        thread.start()
        return {"status": "ok", "job": self.get_cloud_rapid_job(job_id)}

    def get_cloud_rapid_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._cloud_rapid_jobs.get(str(job_id or ""))
            if not job:
                raise KeyError("任务不存在")
            return copy.deepcopy(job)

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
                concurrency=int(result.get("concurrency") or task.get("concurrency") or CLOUD_RAPID_DEFAULT_CONCURRENCY),
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

    def _run_cloud_rapid_transfer(self, task: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
        source_client = self.get_client_by_cookie(task["source_cookie"])
        target_client = self.get_client_by_cookie(task["target_cookie"])
        target_cid = str(task.get("target_cid") or "")
        target_path = str(task.get("target_path") or "")
        concurrency = max(1, min(CLOUD_RAPID_MAX_CONCURRENCY, int(task.get("concurrency") or CLOUD_RAPID_DEFAULT_CONCURRENCY)))
        counters = {"success": 0, "failed": 0, "skipped": 0, "folders": 0, "total_files": 0, "processed": 0}
        results: list[dict[str, Any]] = []
        file_tasks: list[dict[str, Any]] = []
        dir_chain_cache: dict[tuple[str, str, str, str], str] = {}
        task_key = f"cloud_rapid:{int(time.time())}:{uuid.uuid4().hex[:8]}"
        progress_lock = threading.Lock()

        self._sync_cloud_rapid_job(
            job_id,
            counters,
            status="running",
            stage="scanning",
            message="正在扫描来源目录",
            concurrency=concurrency,
        )
        for item in task["items"]:
            try:
                if item["type"] == "dir":
                    self._cloud_rapid_collect_dir(
                        source_client,
                        target_client,
                        item,
                        target_cid,
                        target_path,
                        file_tasks,
                        counters,
                        results,
                        task_key,
                        dir_chain_cache,
                        job_id=job_id,
                    )
                else:
                    self._cloud_rapid_collect_file(
                        item,
                        target_cid,
                        str(item.get("name") or ""),
                        file_tasks,
                        counters,
                        job_id=job_id,
                    )
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

        if file_tasks:
            self._sync_cloud_rapid_job(
                job_id,
                counters,
                status="running",
                stage="transferring",
                current="",
                message=f"扫描完成，开始秒传，并发 {concurrency}",
            )
            if concurrency <= 1 or len(file_tasks) <= 1:
                for file_task in file_tasks:
                    self._execute_cloud_rapid_file_task(
                        source_client,
                        target_client,
                        file_task,
                        counters,
                        results,
                        job_id,
                        progress_lock,
                    )
            else:
                with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="cloud-rapid-file") as executor:
                    futures = [
                        executor.submit(
                            self._execute_cloud_rapid_file_task,
                            source_client,
                            target_client,
                            file_task,
                            counters,
                            results,
                            job_id,
                            progress_lock,
                        )
                        for file_task in file_tasks
                    ]
                    for future in as_completed(futures):
                        future.result()

        status = "ok" if counters["failed"] == 0 and counters["skipped"] == 0 else (
            "partial" if counters["success"] or counters["skipped"] else "error"
        )
        summary = (
            f"总文件 {counters['total_files']}，成功 {counters['success']}，"
            f"跳过 {counters['skipped']}，失败 {counters['failed']}，目录 {counters['folders']}，并发 {concurrency}"
        )
        return {
            "status": status,
            "summary": summary,
            "success": counters["success"],
            "failed": counters["failed"],
            "skipped": counters["skipped"],
            "folders": counters["folders"],
            "total_files": counters["total_files"],
            "processed": counters["processed"],
            "concurrency": concurrency,
            "results": results[:CLOUD_RAPID_RESULT_LIMIT],
            "truncated": len(results) > CLOUD_RAPID_RESULT_LIMIT,
        }

    def _prune_cloud_rapid_jobs_locked(self) -> None:
        now = int(time.time())
        finished = [
            (str(job_id), int(job.get("finished_at") or job.get("updated_at") or 0))
            for job_id, job in self._cloud_rapid_jobs.items()
            if str(job.get("status") or "") in {"success", "partial", "error"}
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
                if str(job.get("status") or "") in {"success", "partial", "error"}
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
        if count_processed:
            counters["processed"] = int(counters.get("processed") or 0) + 1
        results.append(record)
        if job_id:
            with self._lock:
                job = self._cloud_rapid_jobs.get(str(job_id))
                if job:
                    job_results = job.setdefault("results", [])
                    if len(job_results) < CLOUD_RAPID_RESULT_LIMIT:
                        job_results.append(copy.deepcopy(record))
                    else:
                        job["truncated"] = True
            self._sync_cloud_rapid_job(
                job_id,
                counters,
                status="running",
                stage="transferring",
                current=str(record.get("path") or record.get("name") or ""),
                message=str(record.get("message") or ""),
            )

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
        }

    def _list_cloud_115_children(self, client, cid: str, include_files: bool, recursive: bool) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        offset = 0
        cid = str(cid or "0").strip() or "0"
        while True:
            resp = client.fs_files_app(
                {
                    "cid": int(cid),
                    "limit": CLOUD_RAPID_LIST_LIMIT,
                    "offset": offset,
                    "fc_mix": 1 if include_files else 0,
                },
                app="android",
                base_url="https://proapi.115.com",
                headers={"user-agent": CLOUD_RAPID_UA},
                timeout=20,
            )
            if not isinstance(resp, dict) or not resp.get("state"):
                raise RuntimeError("读取 115 目录失败")
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
        is_dir = item.get("is_dir") is True or str(item.get("fc", "") or "") == "0"
        if is_dir:
            return {
                "type": "dir",
                "id": item_id,
                "cid": item_id,
                "name": name or item_id,
            }
        size = int(item.get("fs") or item.get("size") or item.get("file_size") or 0)
        return {
            "type": "file",
            "id": item_id,
            "file_id": item_id,
            "name": name or item_id,
            "size": size,
            "pickcode": str(item.get("pc") or item.get("pickcode") or item.get("pick_code") or "").strip(),
            "sha1": str(item.get("sha1") or item.get("sha") or "").strip().upper(),
        }

    def _list_cloud_115_tree_entries(self, client, cid: str) -> list[dict[str, Any]]:
        from app.services.p115_tree_iter import iter_tree_with_path_by_lists

        entries = list(iter_tree_with_path_by_lists(
            client,
            cid=int(str(cid or "0").strip() or 0),
            with_ancestors=True,
            app="android",
            max_workers=0,
            timeout=30,
        ))
        normalized: list[dict[str, Any]] = []
        for raw in entries:
            entry = self._normalize_cloud_115_entry(raw)
            if not entry:
                continue
            relpath = self._cloud_tree_entry_relpath(raw, entry)
            entry["path"] = relpath or entry.get("name", "")
            entry["relpath"] = relpath or entry.get("name", "")
            entry["parent_id"] = str(raw.get("parent_id") or raw.get("pid") or "")
            normalized.append(entry)
        return normalized

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
        if counters["total_files"] >= CLOUD_RAPID_MAX_FILES:
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
        target_dir_map: dict[str, str] = {"": str(target_cid)}
        dirs = sorted(
            [entry for entry in entries if entry.get("type") == "dir"],
            key=lambda entry: str(entry.get("relpath") or entry.get("path") or "").count("/"),
        )
        files = [entry for entry in entries if entry.get("type") == "file"]
        for child in dirs:
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
        if counters["total_files"] >= CLOUD_RAPID_MAX_FILES:
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
        download_url = self._get_cloud_download_url(source_client, pickcode)
        if not download_url:
            raise RuntimeError(f"无法获取来源直链: {filename}")

        def read_range_callback(sign_check: str) -> str:
            return self._read_cloud_range_sha1(download_url, sign_check)

        result = target_client.upload_file_init(
            filename=filename,
            filesize=int(info["size"]),
            filesha1=str(info["sha1"]).upper(),
            read_range_bytes_or_hash=read_range_callback,
            pid=int(target_cid),
            async_=False,
            timeout=30,
        )
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
        return info

    def _get_cloud_download_url(self, client, pickcode: str) -> str:
        resp = client.download_url_app(
            {"pickcode": str(pickcode or "").strip()},
            user_agent=CLOUD_RAPID_UA,
            app="chrome",
            async_=False,
            timeout=15,
        )
        if not isinstance(resp, dict) or not resp.get("state"):
            return ""
        data = resp.get("data") or {}
        if isinstance(data, dict) and "url" in data:
            return self._extract_download_url_value(data.get("url"))
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, dict):
                    url = self._extract_download_url_value(value.get("url") or value)
                    if url:
                        return url
        return ""

    def _extract_download_url_value(self, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            return str(value.get("url") or "").strip()
        return ""

    def _read_cloud_range_sha1(self, download_url: str, sign_check: str) -> str:
        try:
            resp = httpx.get(
                download_url,
                headers={"Range": f"bytes={sign_check}", "User-Agent": CLOUD_RAPID_UA},
                timeout=15,
                verify=False,
                follow_redirects=True,
            )
            if resp.status_code not in (200, 206):
                return ""
            return hashlib.sha1(resp.content).hexdigest().upper()
        except Exception as e:
            logger.warning(f"[CloudRapid] 范围校验失败: {e}")
            return ""

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
