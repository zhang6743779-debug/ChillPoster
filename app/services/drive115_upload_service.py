from __future__ import annotations

import copy
import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from p115client import P115Client

from app.services.media_organize_115_ops import _check_and_move
from core.logger import logger


CONFIG_FILE = Path("config/drive115_upload_tasks.json")
STATE_FILE = Path("config/drive115_upload_state.json")
CONFIG_302_FILE = Path("config/config_302.json")
WATCH_SCAN_INTERVAL_SECONDS = 2
FILE_STABLE_SECONDS = 5
MAX_QUEUE_SIZE = 200
MAX_HISTORY = 100
DEFAULT_UPLOAD_WORKERS = 1
MAX_UPLOAD_WORKERS = 2
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

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._load_locked()
            self._stop_event.clear()
            self._started = True
            self._ensure_workers_locked()
            self._sync_watchers_locked()
        logger.info("[Drive115Upload] 本地监听上传服务已启动")

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
        for task in self._tasks:
            task_id = str(task.get("id") or "")
            if task_id:
                self._ensure_task_state_locked(task_id)
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
        concurrency = int(payload.get("concurrency") or 1)
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
            "include_existing_on_start": bool(payload.get("include_existing_on_start", False)),
            "delete_local_after_success": bool(payload.get("delete_local_after_success", False)),
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
        logger.info(f"[Drive115Upload] 监听任务启动: {task_id}")
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
        for path in self._iter_candidate_files(task):
            info = self._get_stable_file_info(path)
            if not info:
                continue
            if self._enqueue_file(task, path, info["size"], info["mtime_ns"], force=force, attempts=1, source="scan"):
                queued += 1
        return queued

    def _iter_candidate_files(self, task: dict[str, Any]):
        folder = str(task.get("local_folder") or "")
        if not os.path.isdir(folder):
            return
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    name = entry.name
                    if name.startswith("."):
                        continue
                    lower = name.lower()
                    if lower in {"thumbs.db", ".ds_store"} or lower.endswith(TEMP_SUFFIXES):
                        continue
                    yield entry.path
                except OSError:
                    continue

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
        key = self._make_key(task_id, path, size, mtime_ns)
        with self._lock:
            if key in self._queued_keys or key in self._active_keys or key in self._completed_keys:
                return False
            if not force and (key in self._seen_keys or key in self._failed_keys):
                return False
            job = {
                "job_id": f"upload_{int(time.time())}_{uuid.uuid4().hex[:8]}",
                "task_id": task_id,
                "task_name": task.get("name", ""),
                "path": path,
                "filename": os.path.basename(path),
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
            return True

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
                    logger.error(f"[Drive115Upload] 上传失败 {job.get('path')}: {e}")
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
        client = self.get_client(int(job.get("drive_index") or 0))
        self._update_active(job, "checking", 10, "正在尝试秒传")
        result = P115MultipartUpload.from_path(
            path,
            pid=int(target_cid),
            filename=filename,
            user_id=client.user_id,
            user_key=client.user_key,
            async_=False,
        )
        if isinstance(result, dict):
            if result.get("state") is False:
                raise RuntimeError(result.get("error") or result.get("message") or "秒传初始化失败")
            file_id = self._extract_file_id(result)
            _check_and_move(client, file_id, target_cid, filename, reused=True)
            self._mark_success(job, method="rapid", message="秒传成功")
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
        _check_and_move(client, file_id, target_cid, filename, reused=False)
        self._mark_success(job, method="multipart", message="真实上传成功")

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
            try:
                os.remove(str(job.get("path") or ""))
                deleted_message = "，已删除本地文件"
            except Exception as e:
                deleted_message = f"，本地删除失败: {e}"
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
            "size": int(job.get("size") or 0),
            "key": str(job.get("key") or ""),
            "target_cid": str(job.get("target_cid") or ""),
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
