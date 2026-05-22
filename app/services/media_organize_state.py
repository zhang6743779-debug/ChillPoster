"""
Shared mutable state for media organize module.

Extracted from app/routers/media_organize.py so that both the router
and any service helpers can import the same singletons without circular
imports.
"""

import asyncio
import threading
from typing import Optional, List


def _get_loop_identity(loop: asyncio.AbstractEventLoop | None) -> int | None:
    return id(loop) if loop is not None else None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE = "config/media_organize.json"
VIDEO_EXTS = {'.mp4', '.mpg', '.mkv', '.mpeg', '.ts', '.vob', '.iso', '.m4v', '.avi', '.3gp', '.wmv', '.webm', '.flv', '.mov', '.m2ts', '.rmvb', '.rm', '.asf', '.f4v', '.m2t', '.mts', '.mpe', '.tp', '.trp', '.divx', '.ogv', '.dv'}
SUBTITLE_EXTS = {'.srt', '.ass', '.ssa', '.sub', '.idx', '.sup'}

# ---------------------------------------------------------------------------
# Threading locks
# ---------------------------------------------------------------------------

_rename_lock = threading.Lock()           # 115 文件移动串行锁
_dir_chain_lock = threading.Lock()        # 115 目录链创建串行锁
_read_lock = threading.Lock()             # 115 读请求串行锁
_sha1_cache_lock = threading.Lock()       # 目标目录 sha1 缓存写入锁
_organize_trigger_lock = threading.Lock() # 115 Life 事件触发整理的并发锁
_target_event_lock = threading.Lock()     # 目标目录事件同步并发锁
_source_poll_lock = threading.Lock()      # 源目录轮询并发锁

# ---------------------------------------------------------------------------
# Global flags
# ---------------------------------------------------------------------------

_organize_running = False        # 整理任务是否正在执行
_organize_done_event: asyncio.Event | None = None  # 整理完成通知事件
_target_event_running = False    # 目标目录事件同步任务是否在运行
_source_poll_running = False     # 源目录轮询任务是否在运行

# ---------------------------------------------------------------------------
# Queue / caches
# ---------------------------------------------------------------------------

_target_event_queue: List[tuple] = []                       # 待处理目标目录事件队列
_target_event_sessions: dict = {}                           # 目标目录新增事件稳定轮询会话
_source_poll_sessions: dict = {}                            # source 目录轮询会话
_recent_organize_strm_paths: dict = {}                      # 整理后自动生成过strm的远端路径，短期去重
_main_event_loop: Optional[asyncio.AbstractEventLoop] = None  # FastAPI 主事件循环
_recent_organized_source_paths: dict = {}                    # 整理移动过的文件 file_id → 时间戳，用于过滤自身产生的事件
_recent_created_target_dir_ids: dict = {}                    # 整理自身新建的目标目录 file_id → 时间戳，用于过滤 new_folder 事件
_self_organized_event_skip_lock = threading.Lock()           # 自身事件过滤摘要锁
_self_organized_event_skip_count = 0                         # 已跳过的整理自身事件数量
_self_organized_event_last_log_at = 0.0                      # 上次输出摘要时间

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def register_main_event_loop(loop: asyncio.AbstractEventLoop):
    """注册 FastAPI 主事件循环，供回调线程投递任务用"""
    global _main_event_loop
    _main_event_loop = loop


def _record_organized_source_path(file_id: str, target_path: str = "", source_path: str = ""):
    """记录整理操作移动的文件 file_id 及目标路径，供事件回调过滤自身产生的事件"""
    import time as _t
    file_id = str(file_id or "")
    if not file_id:
        return
    source_path = str(source_path or "").rstrip("/")
    source_parent = source_path.rsplit("/", 1)[0] if "/" in source_path else ""
    _recent_organized_source_paths[file_id] = {
        "path": str(target_path or "").rstrip("/"),
        "source_path": source_path,
        "source_parent": source_parent,
        "ts": _t.time(),
    }
    if len(_recent_organized_source_paths) > 3000:
        now = _t.time()
        stale = [k for k, v in _recent_organized_source_paths.items() if now - v["ts"] > 120]
        for k in stale:
            _recent_organized_source_paths.pop(k, None)


def _is_self_organized_event(file_id: str, current_path: str) -> bool:
    """检查事件是否由整理自身产生。115 会先发源目录 rename，再发目标目录 move。"""
    import time as _t
    from core.logger import logger
    file_id = str(file_id or "")
    if not file_id:
        return False
    now = _t.time()
    stale = [k for k, v in _recent_organized_source_paths.items() if now - v["ts"] > 120]
    for k in stale:
        _recent_organized_source_paths.pop(k, None)
    entry = _recent_organized_source_paths.get(file_id)
    if not entry:
        return False
    current_path = str(current_path or "").rstrip("/")
    recorded_path = str(entry.get("path", "") or "").rstrip("/")
    source_path = str(entry.get("source_path", "") or "").rstrip("/")
    source_parent = str(entry.get("source_parent", "") or "").rstrip("/")
    matched = bool(
        current_path
        and (
            (recorded_path and (current_path == recorded_path or current_path.startswith(recorded_path + "/")))
            or (source_path and current_path == source_path)
            or (source_parent and current_path.startswith(source_parent + "/"))
        )
    )
    if matched:
        logger.trace(f"[115Life] 自组织过滤命中: file_id={file_id}, current_path={current_path}")
    return matched


def _record_self_organized_event_skip(event_name: str = "", *, force: bool = False) -> None:
    """聚合输出整理自身产生的 115 Life 事件过滤数量，避免大批量整理时刷屏。"""
    import time as _t
    from core.logger import logger

    global _self_organized_event_skip_count, _self_organized_event_last_log_at

    now = _t.time()
    with _self_organized_event_skip_lock:
        _self_organized_event_skip_count += 1
        elapsed = now - _self_organized_event_last_log_at
        if not _self_organized_event_last_log_at:
            _self_organized_event_last_log_at = now
            return
        should_log = force or _self_organized_event_skip_count >= 100 or elapsed >= 30
        if not should_log:
            return
        count = _self_organized_event_skip_count
        _self_organized_event_skip_count = 0
        _self_organized_event_last_log_at = now

    suffix = f"，最近事件={event_name}" if event_name else ""
    logger.debug(f"[115Life] 已跳过整理自身产生的事件 {count} 条{suffix}")


def _record_created_target_dir_id(file_id: str):
    """记录整理自身新建的目标目录 file_id，供监控线程过滤 new_folder 事件"""
    import time as _t
    file_id = str(file_id or "")
    if not file_id:
        return
    _recent_created_target_dir_ids[file_id] = {"ts": _t.time()}
    if len(_recent_created_target_dir_ids) > 3000:
        now = _t.time()
        stale = [k for k, v in _recent_created_target_dir_ids.items() if now - v["ts"] > 120]
        for k in stale:
            _recent_created_target_dir_ids.pop(k, None)


def _is_recent_created_target_dir_id(file_id: str) -> bool:
    """检查 file_id 是否命中整理自身新建的目标目录短期缓存"""
    file_id = str(file_id or "")
    if not file_id:
        return False
    import time as _t
    now = _t.time()
    stale = [k for k, v in _recent_created_target_dir_ids.items() if now - v["ts"] > 120]
    for k in stale:
        _recent_created_target_dir_ids.pop(k, None)
    return file_id in _recent_created_target_dir_ids
