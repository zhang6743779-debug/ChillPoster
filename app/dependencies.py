# app/dependencies.py
import os
import json
import uuid
import time
import gc
import queue
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

# 引入配置
from core.configs import CONFIG_FILE, TRANSLATIONS_FILE, TASK_PROGRESS_FILE
import core.tmdb as tmdb_module
import core.douban as douban_module
from core.logger import logger

# 全局线程池
MAX_WORKERS = min(4, (os.cpu_count() or 1) + 1)
GLOBAL_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# 任务状态存储
ACTIVE_TASKS = {}
TASK_STATUS_TERMINAL = {"finished", "error", "stopped", "interrupted"}
TERMINAL_TASK_RETENTION_SECONDS = 300
RUNNING_TASK_HEARTBEAT_TIMEOUT_SECONDS = 180
TASK_PROGRESS_SAVE_INTERVAL_SECONDS = 1.0
_TASK_PROGRESS_LOCK = threading.RLock()
_TASK_PROGRESS_LAST_SAVE_AT = 0.0
_TASK_PROGRESS_LOADED = False

# RSS 任务队列
RSS_JOB_QUEUE = queue.Queue()

# 全局翻译缓存
global_translations = {}

def load_translations_data():
    """加载翻译数据"""
    global global_translations
    if os.path.exists(TRANSLATIONS_FILE):
        try:
            with open(TRANSLATIONS_FILE, "r", encoding="utf-8") as f:
                global_translations = json.load(f)
        except:
            pass
    return global_translations

# 初始化加载一次
load_translations_data()

def _infer_task_type(run_id, name=""):
    run_id_str = str(run_id or "")
    task_name = str(name or "")
    if run_id_str.startswith("organize_") or "整理" in task_name:
        return "media_organize"
    if run_id_str.startswith("rss_run_") or task_name.startswith("RSS"):
        return "rss"
    if run_id_str.startswith("upgrade_") or "升级" in task_name:
        return "upgrade"
    if task_name.startswith("STRM"):
        return "strm"
    if "备份" in task_name:
        return "backup"
    if task_name.startswith("任务:"):
        return "preset_task"
    return "generic"


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _coerce_float(value, default=0.0):
    try:
        number = float(value)
        if number != number:
            return default
        return number
    except Exception:
        return default


def _coerce_pid(value):
    try:
        pid = int(value)
        return pid if pid > 0 else None
    except Exception:
        return None


def _serialize_task_record(task):
    record = {}
    for key, value in (task or {}).items():
        if key == "percent":
            record[key] = _coerce_float(value, 0.0)
        elif key in {"updated_at", "started_at", "heartbeat_at", "completed_at", "interrupted_at"}:
            record[key] = _coerce_float(value, 0.0)
        elif key == "pid":
            record[key] = _coerce_pid(value)
        else:
            record[key] = _json_safe(value)
    return record


def _save_task_progress_locked(force=False):
    global _TASK_PROGRESS_LAST_SAVE_AT
    now = time.time()
    if not force and now - _TASK_PROGRESS_LAST_SAVE_AT < TASK_PROGRESS_SAVE_INTERVAL_SECONDS:
        return
    os.makedirs(os.path.dirname(TASK_PROGRESS_FILE), exist_ok=True)
    tmp_path = f"{TASK_PROGRESS_FILE}.tmp"
    data = {str(run_id): _serialize_task_record(task) for run_id, task in ACTIVE_TASKS.items()}
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, TASK_PROGRESS_FILE)
    _TASK_PROGRESS_LAST_SAVE_AT = now


def persist_task_progress(force=False):
    with _TASK_PROGRESS_LOCK:
        _save_task_progress_locked(force=force)


def snapshot_task_progress():
    """返回任务状态的轻量内存快照，供 /api/progress 只读使用。"""
    with _TASK_PROGRESS_LOCK:
        snapshot = {}
        for run_id, task in ACTIVE_TASKS.items():
            item = dict(task or {})
            detail = item.get("detail")
            if isinstance(detail, dict):
                item["detail"] = dict(detail)
            elif isinstance(detail, list):
                item["detail"] = list(detail)
            snapshot[str(run_id)] = item
        return snapshot


def _normalize_task_record(run_id, raw, now=None):
    if not isinstance(raw, dict):
        return None
    now = now or time.time()
    name = str(raw.get("name") or "任务")
    status = str(raw.get("status") or "running")
    if status not in TASK_STATUS_TERMINAL and status != "running":
        status = "running"
    updated_at = _coerce_float(raw.get("updated_at"), now)
    started_at = _coerce_float(raw.get("started_at"), updated_at or now)
    heartbeat_at = _coerce_float(raw.get("heartbeat_at"), updated_at if status == "running" else 0.0)
    task_type = str(raw.get("task_type") or _infer_task_type(run_id, name))
    record = {
        "name": name,
        "percent": _coerce_float(raw.get("percent"), 0.0),
        "status": status,
        "updated_at": updated_at,
        "started_at": started_at,
        "pid": _coerce_pid(raw.get("pid")),
        "heartbeat_at": heartbeat_at,
        "cancel_requested": bool(raw.get("cancel_requested", False)),
        "task_type": task_type,
    }
    for key in ("detail", "completed_at", "interrupted_at", "interrupted_reason", "resume_message"):
        if key in raw and raw.get(key) is not None:
            record[key] = _json_safe(raw.get(key))
    return record


def _mark_task_interrupted_locked(run_id, task, now=None, reason="服务重启或心跳超时"):
    now = now or time.time()
    record = dict(task or {})
    task_type = record.get("task_type") or _infer_task_type(run_id, record.get("name", ""))
    name = str(record.get("name") or "任务")
    if task_type == "media_organize":
        interrupted_name = "整理已中断: 服务重启或心跳超时"
    elif "已中断" in name:
        interrupted_name = name
    else:
        interrupted_name = f"{name} (已中断)"
    record.update({
        "name": interrupted_name,
        "status": "interrupted",
        "updated_at": now,
        "completed_at": now,
        "interrupted_at": now,
        "interrupted_reason": reason,
        "resume_message": "已中断，正在重新扫描续跑" if task_type == "media_organize" else "服务重启后任务已中断",
        "cancel_requested": False,
        "task_type": task_type,
    })
    return record


def load_task_progress_from_file():
    """恢复上次进程留下的任务状态，并把旧 running 任务标记为 interrupted。"""
    global _TASK_PROGRESS_LOADED
    with _TASK_PROGRESS_LOCK:
        if _TASK_PROGRESS_LOADED:
            return
        _TASK_PROGRESS_LOADED = True
        if not os.path.exists(TASK_PROGRESS_FILE):
            return
        try:
            with open(TASK_PROGRESS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            logger.warning(f"[Tasks] 任务进度恢复失败: {e}")
            return

        if isinstance(raw_data, dict) and isinstance(raw_data.get("tasks"), dict):
            raw_data = raw_data.get("tasks")
        if not isinstance(raw_data, dict):
            return

        now = time.time()
        current_pid = os.getpid()
        loaded_count = 0
        interrupted_count = 0
        for run_id, raw_task in raw_data.items():
            record = _normalize_task_record(run_id, raw_task, now=now)
            if not record:
                continue
            heartbeat_at = _coerce_float(record.get("heartbeat_at"), 0.0)
            old_pid = _coerce_pid(record.get("pid"))
            heartbeat_stale = not heartbeat_at or now - heartbeat_at > RUNNING_TASK_HEARTBEAT_TIMEOUT_SECONDS
            process_changed = bool(old_pid and old_pid != current_pid)
            if record.get("status") == "running" and (process_changed or heartbeat_stale):
                record = _mark_task_interrupted_locked(run_id, record, now=now, reason="服务重启或心跳超时")
                interrupted_count += 1
            ACTIVE_TASKS[str(run_id)] = record
            loaded_count += 1

        _cleanup_stale_tasks_locked(now)
        _save_task_progress_locked(force=True)
        if loaded_count:
            logger.info(f"[Tasks] 已恢复任务进度: {loaded_count} 条，中断 {interrupted_count} 条")


def get_recent_interrupted_task(task_type, within_seconds=86400):
    now = time.time()
    with _TASK_PROGRESS_LOCK:
        candidates = [
            (run_id, task)
            for run_id, task in ACTIVE_TASKS.items()
            if task.get("status") == "interrupted"
            and task.get("task_type") == task_type
            and now - _coerce_float(task.get("interrupted_at") or task.get("updated_at"), 0.0) <= within_seconds
        ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: _coerce_float(item[1].get("interrupted_at") or item[1].get("updated_at"), 0.0), reverse=True)
    run_id, task = candidates[0]
    return {"run_id": run_id, **task}


def set_task_detail(run_id, detail, force=False):
    with _TASK_PROGRESS_LOCK:
        task = ACTIVE_TASKS.get(run_id)
        if task is None:
            return False
        task["detail"] = _json_safe(detail or {})
        task["updated_at"] = time.time()
        _save_task_progress_locked(force=force)
        return True


def remove_task_progress(run_id):
    with _TASK_PROGRESS_LOCK:
        existed = run_id in ACTIVE_TASKS
        if existed:
            del ACTIVE_TASKS[run_id]
            _save_task_progress_locked(force=True)
        return existed


def request_task_cancel(run_id):
    with _TASK_PROGRESS_LOCK:
        task = ACTIVE_TASKS.get(run_id)
        if not task:
            return None
        if task.get("status") in TASK_STATUS_TERMINAL:
            return dict(task)
        task["cancel_requested"] = True
        task["updated_at"] = time.time()
        _save_task_progress_locked(force=True)
        return dict(task)


def update_task_progress(run_id, name, percent, status="running", detail=None):
    """更新任务进度"""
    if not run_id:
        return
    now = time.time()
    status = status if status in TASK_STATUS_TERMINAL or status == "running" else "running"
    with _TASK_PROGRESS_LOCK:
        cancel_req = False
        existing_detail = None
        started_at = now
        task_type = _infer_task_type(run_id, name)
        existing = ACTIVE_TASKS.get(run_id)
        if existing:
            cancel_req = existing.get("cancel_requested", False)
            existing_detail = existing.get("detail")
            started_at = _coerce_float(existing.get("started_at"), now)
            task_type = existing.get("task_type") or task_type

        task = {
            "name": name,
            "percent": _coerce_float(percent, 0.0),
            "status": status,
            "updated_at": now,
            "started_at": started_at,
            "pid": os.getpid(),
            "heartbeat_at": now if status == "running" else _coerce_float((existing or {}).get("heartbeat_at"), now),
            "cancel_requested": cancel_req,
            "task_type": task_type,
        }
        if detail is not None:
            task["detail"] = _json_safe(detail)
        elif existing_detail is not None:
            task["detail"] = existing_detail
        if status in TASK_STATUS_TERMINAL:
            task["completed_at"] = now
        for key in ("interrupted_at", "interrupted_reason", "resume_message"):
            if existing and key in existing:
                task[key] = existing[key]

        ACTIVE_TASKS[run_id] = task
        _save_task_progress_locked(force=status in TASK_STATUS_TERMINAL)

def cleanup_stale_tasks():
    """清理过期任务记录"""
    with _TASK_PROGRESS_LOCK:
        changed = _cleanup_stale_tasks_locked(time.time())
        if changed:
            _save_task_progress_locked(force=True)
            gc.collect()


def _cleanup_stale_tasks_locked(now):
    changed = False
    current_pid = os.getpid()
    for run_id, task in list(ACTIVE_TASKS.items()):
        updated_at = _coerce_float(task.get("updated_at"), 0.0)
        if task.get("status") == "running":
            heartbeat_at = _coerce_float(task.get("heartbeat_at"), updated_at)
            old_pid = _coerce_pid(task.get("pid"))
            if old_pid and old_pid != current_pid and now - heartbeat_at > RUNNING_TASK_HEARTBEAT_TIMEOUT_SECONDS:
                ACTIVE_TASKS[run_id] = _mark_task_interrupted_locked(run_id, task, now=now, reason="心跳超时")
                changed = True
                continue
        if task.get("status") in TASK_STATUS_TERMINAL and now - updated_at > TERMINAL_TASK_RETENTION_SECONDS:
            del ACTIVE_TASKS[run_id]
            changed = True
        elif now - updated_at > 86400:
            del ACTIVE_TASKS[run_id]
            changed = True
    return changed

def apply_proxy_settings():
    """应用代理设置"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            proxy_url = settings.get('proxy_url', '')
            tmdb_module.set_proxy(proxy_url)
            douban_module.DoubanApi.set_proxy(proxy_url)
            logger.info(f"[System] 代理配置已更新: {proxy_url if proxy_url else '关闭'}")
        except Exception as e:
            logger.error(f"[System] 应用代理失败: {e}")

from threading import Timer
from core.configs import DEVICE_ID_FILE

# Webhook 防抖管理器
class WebhookDebouncer:
    def __init__(self, delay=10):
        self.delay = delay 
        self.timers = {}
        self.lock = threading.Lock()

    def schedule(self, library_id, func, *args, display_name=None, **kwargs):
        with self.lock:
            if library_id in self.timers:
                self.timers[library_id].cancel()

            timer = Timer(self.delay, self._run, args=(library_id, func, args, kwargs))
            self.timers[library_id] = timer
            timer.start()
            lib_text = display_name or library_id
            logger.info(f"[Webhook] 已安排刷新封面 媒体库: {lib_text} ({self.delay}s后)")

    def _run(self, library_id, func, args, kwargs):
        with self.lock:
            if library_id in self.timers:
                del self.timers[library_id]
        func(*args[0], **kwargs)

# 全局防抖实例
webhook_debouncer = WebhookDebouncer(delay=10)


def format_episode_range(episodes: list) -> str:
    """将集数列表格式化为连续区间字符串。
    例如: [1,2,3,5,6,8] -> "E01-E03,E05-E06,E08"
         [1] -> "E01"
         [1,2] -> "E01-E02"
    """
    if not episodes:
        return ""
    # 只保留能转成整数的集数
    nums = sorted(set(int(e) for e in episodes if str(e).lstrip('-').isdigit()))
    if not nums:
        return ""

    ranges = []
    start = end = nums[0]
    for ep in nums[1:]:
        if ep == end + 1:
            end = ep
        else:
            ranges.append((start, end))
            start = end = ep
    ranges.append((start, end))

    parts = []
    for s, e in ranges:
        if s == e:
            parts.append(f"E{s:02d}")
        else:
            parts.append(f"E{s:02d}-E{e:02d}")
    return ",".join(parts)


class EpisodeNotifyAggregator:
    """剧集入库通知聚合器：将短时间内同一剧集同一季的多集合并成一条通知发送。"""

    def __init__(self, delay: int = 15):
        self.delay = delay
        self.lock = threading.Lock()
        self.buckets: dict = {}  # key -> {"timer": Timer, "episodes": list, "meta": dict}

    def add(self, key: str, episode_num, meta: dict):
        """将一集加入待聚合队列，重置该 key 的防抖计时器。"""
        with self.lock:
            if key in self.buckets:
                self.buckets[key]["timer"].cancel()
                self.buckets[key]["episodes"].append(episode_num)
            else:
                self.buckets[key] = {"episodes": [episode_num], "meta": meta}
            t = Timer(self.delay, self._flush, args=(key,))
            self.buckets[key]["timer"] = t
            t.start()

    def _flush(self, key: str):
        """计时器触发后，聚合该 key 的所有集数并发送一条通知。"""
        with self.lock:
            bucket = self.buckets.pop(key, None)
        if not bucket:
            return

        episodes = bucket["episodes"]
        meta = bucket["meta"]
        series_name = meta["series_name"]
        season = meta["season"]

        range_str = format_episode_range(episodes)
        if range_str:
            season_str = str(season).zfill(2) if str(season).isdigit() else str(season)
            media_name = f"{series_name} S{season_str} {range_str}"
        else:
            # fallback：直接拼原始集号
            season_str = str(season).zfill(2) if str(season).isdigit() else str(season)
            ep_str = str(episodes[0]).zfill(2) if str(episodes[0]).isdigit() else str(episodes[0])
            media_name = f"{series_name} S{season_str}E{ep_str}"

        notify_kwargs = dict(
            media_name=media_name,
            media_type="series",
            library_name=meta.get("library_name", ""),
            year=meta.get("year", ""),
            poster_url=meta.get("poster_url", ""),
            original_name=meta.get("original_name", ""),
            overview=meta.get("overview", ""),
            rating=meta.get("rating", ""),
            genres=meta.get("genres", ""),
            tagline=meta.get("tagline", ""),
            status=meta.get("status", ""),
            premiere_date=meta.get("premiere_date", ""),
            item_count=str(len(episodes)),
            server_name=meta.get("server_name", ""),
            tmdb_url=meta.get("tmdb_url", ""),
            original_title=meta.get("original_title", ""),
            server_idx=meta.get("server_idx", 0),
            item_id=meta.get("item_id", ""),
        )

        try:
            from app.services.wechat_service import wechat_notify_service
            from app.services.telegram_service import telegram_notify_service
            wechat_notify_service.notify_media_added(**notify_kwargs)
            telegram_notify_service.notify_media_added(**notify_kwargs)
            logger.info(f"[EpisodeNotify] 聚合通知已发送: {media_name}")
        except Exception as e:
            logger.error(f"[EpisodeNotify] 发送聚合通知失败: {e}")


# 全局剧集聚合器实例
episode_notify_aggregator = EpisodeNotifyAggregator(delay=15)

def get_device_fingerprint():
    """获取或生成设备指纹"""
    if os.path.exists(DEVICE_ID_FILE):
        try:
            with open(DEVICE_ID_FILE, 'r', encoding='utf-8') as f:
                code = f.read().strip()
                if code and len(code) >= 8: return code
        except: pass
    try: 
        new_code = uuid.uuid4().hex[:12].upper()
    except: 
        new_code = "UNKNOWN-DEVICE"
    try:
        with open(DEVICE_ID_FILE, 'w', encoding='utf-8') as f: f.write(new_code)
    except: pass
    return new_code
