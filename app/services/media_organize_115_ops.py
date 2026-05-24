"""
115 网盘操作函数 —— 从 app/routers/media_organize.py 提取
包含：客户端获取、目录/文件操作、上传、媒体库缓存辅助、视频/字幕扫描等。
"""

import os
import re
import json
import time
import random
import inspect
import asyncio
import threading
import itertools
import queue
import concurrent.futures
from contextlib import asynccontextmanager
from typing import Optional, Iterator, Callable, Any

from core.logger import logger
from core.media_library_cache import get_dir_by_parent_and_name, get_dir_by_path, upsert_dir_item
from app.services.media_organize_state import (
    _read_lock,
    VIDEO_EXTS,
    SUBTITLE_EXTS,
    CONFIG_FILE,
    _record_organized_source_path,
    _record_created_target_dir_id,
)


# ==========================================
# 115 客户端 / 文件系统
# ==========================================

def _get_115_client(drive_index: int = 0):
    """从 config_302.json 获取 P115Client"""
    cfg_path = "config/config_302.json"
    with open(cfg_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)

    drives = cfg.get("drives", [])
    drive_cfg = drives[0] if isinstance(drives, list) and drives else cfg.get("drive", {})
    if not isinstance(drive_cfg, dict) or not drive_cfg:
        raise ValueError("未配置 115 账号")

    cookie = str(drive_cfg.get("cookie", "") or "").strip()
    if not cookie:
        raise ValueError("Cookie 未配置")

    from p115client import P115Client
    return P115Client(cookie, app="android")


def _get_115_fs(client):
    from p115client.fs import P115FileSystem
    return P115FileSystem(client)


def _prime_115_pickcode_stable_point(client, cid: str) -> None:
    if getattr(client, "__dict__", {}).get("pickcode_stable_point"):
        return
    try:
        from p115pickcode import get_stable_point
        resp = client.fs_category_get(str(cid))
        if not resp or not resp.get("state"):
            return
        pickcode = str(resp.get("pick_code") or resp.get("pickcode") or "")
        if pickcode:
            client.__dict__["pickcode_stable_point"] = get_stable_point(pickcode)
            logger.debug(f"[MediaOrganize] 已用目录 pickcode 初始化稳定点: cid={cid}")
    except Exception as e:
        logger.debug(f"[MediaOrganize] 初始化 pickcode 稳定点失败: cid={cid}, error={e}")


def _get_115_file_name(client, file_cid: str) -> str:
    """通过文件 cid 获取文件名"""
    try:
        fs = _get_115_fs(client)
        with _read_lock:
            attr = fs._get_attr_by_id(int(file_cid))
        return attr.get("name", "")
    except Exception as e:
        logger.warning(f"[MediaOrganize] 获取文件名失败: {e}")
    return ""


# ==========================================
# 目录操作
# ==========================================

_dir_create_locks_guard = threading.Lock()
_dir_create_locks: dict[tuple[str, str, str], threading.Lock] = {}


def _get_dir_create_lock(task_key: str, parent_cid: str, name: str, dir_path: str = "") -> threading.Lock:
    normalized_path = str(dir_path or "").rstrip("/")
    if task_key and normalized_path:
        lock_key = ("path", str(task_key), normalized_path)
    else:
        lock_key = ("parent", str(parent_cid), str(name or ""))
    with _dir_create_locks_guard:
        lock = _dir_create_locks.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _dir_create_locks[lock_key] = lock
        return lock


def _extract_created_dir_info(resp: dict) -> tuple[str, str]:
    data = (resp or {}).get("data") or {}
    cid = str(
        data.get("category_id")
        or data.get("cid")
        or resp.get("cid")
        or resp.get("id")
        or ""
    )
    pickcode = str(
        data.get("pick_code")
        or data.get("pickcode")
        or resp.get("pick_code")
        or resp.get("pickcode")
        or ""
    )
    return cid, pickcode


def _find_cached_dir(task_key: str, parent_id: int, name: str, dir_path: str = "") -> tuple[str, str, str]:
    if not task_key:
        return "", "", ""
    normalized_path = str(dir_path or "").rstrip("/")
    if normalized_path:
        path_match = get_dir_by_path(task_key, normalized_path)
        if path_match:
            cid, pickcode = path_match
            return str(cid or ""), str(pickcode or ""), "path"
    parent_match = get_dir_by_parent_and_name(task_key, parent_id, name)
    if parent_match:
        cid, pickcode = parent_match
        return str(cid or ""), str(pickcode or ""), "parent+name"
    return "", "", ""


def _find_existing_115_child_dir(client, parent_cid: str, name: str) -> tuple[str, str]:
    fs = _get_115_fs(client)
    with _read_lock:
        children = list(fs.iterdir(int(parent_cid)))
    for child in children:
        child_name = str(child.get("name", "") or "")
        is_dir = child.get("is_dir") is True or str(child.get("fc", "") or "") == "0"
        if is_dir and child_name == name:
            cid = str(child.get("id") or child.get("cid") or child.get("category_id") or "")
            pickcode = str(child.get("pickcode") or child.get("pick_code") or "")
            if cid:
                return cid, pickcode
    return "", ""


def _mkdir_115_dir(client, parent_cid: str, name: str, task_key: str = "", dir_path: str = "") -> tuple[str, str]:
    with _get_dir_create_lock(task_key, parent_cid, name, dir_path):
        parent_id = int(parent_cid) if str(parent_cid).isdigit() else 0
        normalized_dir_path = str(dir_path or "").rstrip("/")

        cid, pickcode, cache_match = _find_cached_dir(task_key, parent_id, name, normalized_dir_path)
        if cid:
            if cache_match == "path":
                logger.trace(f"[CategoryDir] 创建前命中缓存(path): path={normalized_dir_path}, cid={cid}")
            else:
                logger.trace(f"[CategoryDir] 创建前命中缓存(parent+name): parent={parent_cid}, name={name}, cid={cid}")
            return str(cid), str(pickcode or "")

        resp = _run_115_write_request_sync(
            client,
            "创建目录",
            lambda write_client: write_client.fs_mkdir_app(
                name,
                pid=int(parent_cid),
                async_=False,
                timeout=_WRITE_REQUEST_TIMEOUT_SECONDS,
            ),
            raise_on_state_false=False,
        )
        if not resp.get("state"):
            error_text = str(resp.get("error", "") or resp.get("message", "") or "")
            if "已存在" in error_text or "exist" in error_text.lower():
                cid, pickcode = _find_existing_115_child_dir(client, parent_cid, name)
                if cid:
                    if task_key and str(cid).isdigit():
                        upsert_dir_item(
                            task_key,
                            int(cid),
                            name,
                            parent_id,
                            pickcode=pickcode,
                            path=normalized_dir_path,
                        )
                    logger.debug(f"[CategoryDir] 目录已存在，扫描父目录补齐缓存: parent={parent_cid}, name={name}, cid={cid}")
                    return str(cid), str(pickcode or "")
                logger.debug(f"[CategoryDir] 目录已存在但未找到同名目录: parent={parent_cid}, name={name}, path={normalized_dir_path}")
            raise RuntimeError(resp)
        cid, pickcode = _extract_created_dir_info(resp)
        if not cid:
            raise RuntimeError(f"创建目录未返回cid: parent={parent_cid}, name={name}, resp={resp}")
        if task_key and str(cid).isdigit():
            upsert_dir_item(
                task_key,
                int(cid),
                name,
                parent_id,
                pickcode=pickcode,
                path=normalized_dir_path,
            )
        _record_created_target_dir_id(cid)
        return cid, pickcode


def _ensure_115_dir_chain_cached(client, base_cid: str, category_path: str,
                                  dir_chain_cache: dict, task_key: str = "", base_path: str = "") -> str:
    """按 category_path（如 '动漫/动画电影/国产'）逐级创建目录链。"""
    normalized_base_path = str(base_path or "").strip().rstrip("/")
    normalized_category_path = "/".join(
        part.strip().strip("/")
        for part in str(category_path or "").split("/")
        if part.strip().strip("/")
    )
    cache_key = (str(task_key or ""), str(base_cid), normalized_base_path, normalized_category_path)

    if dir_chain_cache is not None:
        cached_cid = str(dir_chain_cache.get(cache_key, "") or "")
        if cached_cid:
            logger.trace(f"[CategoryDir] 目录链命中任务缓存: {normalized_category_path} (cid={cached_cid})")
            return cached_cid

    current_parent_cid = str(base_cid)
    current_path = normalized_base_path
    final_cid = ""

    for segment in normalized_category_path.split("/"):
        if not segment:
            continue
        dir_path = f"{current_path}/{segment}" if current_path else segment
        child_cid, child_pickcode = _mkdir_115_dir(
            client,
            current_parent_cid,
            segment,
            task_key=task_key,
            dir_path=dir_path,
        )
        current_parent_cid = str(child_cid)
        current_path = dir_path
        final_cid = str(child_cid)

    if final_cid:
        if dir_chain_cache is not None:
            dir_chain_cache[cache_key] = final_cid
        logger.debug(f"[CategoryDir] 目录链已确认: {normalized_category_path} (cid={final_cid})")
        return final_cid

    raise RuntimeError(f"创建分类目录失败: {category_path}")


def _try_remove_empty_dir(client, dir_cid: str):
    """检查目录是否为空（无视频和字幕文件），是则删除"""
    fs = _get_115_fs(client)
    try:
        items = list(fs.iterdir(int(dir_cid)))
        if not items:
            run_115_write_request_sync(
                client,
                "删除空目录",
                lambda write_client: write_client.fs_delete([int(dir_cid)], async_=False),
            )
            return
        has_media = any(
            item.get("fc") != "0" and
            os.path.splitext(item.get("name", ""))[1].lower() in (VIDEO_EXTS | SUBTITLE_EXTS)
            for item in items
        )
        if not has_media:
            run_115_write_request_sync(
                client,
                "删除非媒体空目录",
                lambda write_client: write_client.fs_delete([int(dir_cid)], async_=False),
            )
    except Exception:
        pass


# ==========================================
# 文件操作（重命名 / 移动 / 字幕 / 上传）
# ==========================================

_WRITE_API_RATE_INTERVAL_SECONDS = 0.5
_DIRECT_URL_PACING_MIN_SECONDS = 1.0
_DIRECT_URL_PACING_MAX_SECONDS = 1.5
DIRECT_URL_PRIORITY_PLAYBACK = 0
DIRECT_URL_PRIORITY_DEFAULT = 50
# STRM /d requests may come from Emby scans, so keep them behind organize ffprobe.
DIRECT_URL_PRIORITY_DIRECT = 80
_DIRECT_URL_LOW_PRIORITY_GRACE_SECONDS = 0.15
_WRITE_API_RATE_LOCK = threading.Lock()
_LAST_WRITE_API_AT = 0.0
_LAST_DIRECT_URL_AT = 0.0
_DIRECT_URL_REQUEST_TIMEOUT_SECONDS = 10
_DIRECT_URL_TIMEOUT_MAX_RETRIES = 1
_DIRECT_URL_QUEUE: "queue.PriorityQueue[tuple[int, int, str, Callable[[], Any], concurrent.futures.Future]]" = queue.PriorityQueue()
_DIRECT_URL_SEQUENCE = itertools.count(1)
_DIRECT_URL_WORKER_THREAD: Optional[threading.Thread] = None
_DIRECT_URL_WORKER_LOCK = threading.Lock()
_WRITE_REQUEST_TIMEOUT_SECONDS = 20
_WRITE_API_RATE_LIMIT_MAX_RETRIES = 3
_WRITE_API_RATE_LIMIT_BACKOFF_SECONDS = (5.0, 10.0, 20.0)
_MOVE_PROGRESS_POLL_INTERVAL_SECONDS = 2.0
_MOVE_PROGRESS_TIMEOUT_SECONDS = 1800.0
_TREE_SCAN_MIN_INTERVAL_SECONDS = 5.0
_TREE_SCAN_LOCK = threading.Lock()
_LAST_TREE_SCAN_FINISHED_AT = 0.0
_FILE_OP_TIMING_INFO_THRESHOLD_SECONDS = 10.0
_FILE_OP_WRITE_SUBMIT_INFO_THRESHOLD_SECONDS = 5.0
_FILE_OP_MOVE_WAIT_INFO_THRESHOLD_SECONDS = 5.0


def _read_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"[115] 环境变量 {name}={raw!r} 无效，使用默认值 {default}")
        return default
    if value < 1:
        logger.warning(f"[115] 环境变量 {name}={raw!r} 必须大于 0，使用默认值 {default}")
        return default
    return value


_WRITE_REQUEST_WORKERS = _read_positive_int_env("CHILLPOSTER_115_WRITE_WORKERS", 4)
_WRITE_REQUEST_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_WRITE_REQUEST_WORKERS,
    thread_name_prefix="chillposter-115-write",
)
_WRITE_LOCK_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="chillposter-115-write-lock",
)


@asynccontextmanager
async def _thread_lock_context(lock: threading.Lock):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_WRITE_LOCK_EXECUTOR, lock.acquire)
    try:
        yield
    finally:
        lock.release()


def _clone_115_client_for_write(client):
    from p115client import P115Client
    return P115Client(str(client.cookies_str), app="android")


def _is_115_rate_limited_response(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    text = str(response)
    return (
        str(response.get("code", "")) == "990009"
        or str(response.get("errno", "")) == "990009"
        or str(response.get("errNo", "")) == "990009"
        or "990009" in text
        or "操作频繁" in text
        or "操作尚未执行完成" in text
    )


def _get_115_rate_limit_backoff_seconds(attempt: int) -> float:
    if 0 <= attempt < len(_WRITE_API_RATE_LIMIT_BACKOFF_SECONDS):
        return _WRITE_API_RATE_LIMIT_BACKOFF_SECONDS[attempt]
    return _WRITE_API_RATE_LIMIT_BACKOFF_SECONDS[-1]


def _extract_115_move_progress_id(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    candidates = [response]
    data = response.get("data")
    if isinstance(data, dict):
        candidates.append(data)
    for payload in candidates:
        for key in ("move_proid", "move_pro_id", "move_id", "pro_id", "proid", "task_id"):
            value = payload.get(key)
            if value:
                return str(value)
    return ""


def _is_115_move_progress_done(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("state") is False:
        raise RuntimeError(f"移动进度查询失败: {response}")
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    status_text = str(
        data.get("status")
        or data.get("state")
        or data.get("status_text")
        or data.get("message")
        or data.get("status_msg")
        or ""
    ).lower().strip()
    if status_text in {"1", "2", "done", "finish", "finished", "success", "complete", "completed"}:
        return True
    if any(token in status_text for token in ("完成", "成功")):
        return True
    for key in ("is_finish", "is_finished", "finished", "done", "complete", "completed", "success"):
        value = data.get(key)
        if value is True or str(value).strip() in {"1", "true", "yes"}:
            return True
    percent = data.get("percent") or data.get("progress") or data.get("rate")
    try:
        if percent is not None and float(str(percent).rstrip("%")) >= 100:
            return True
    except (TypeError, ValueError):
        pass
    total = data.get("total") or data.get("count") or data.get("all_count")
    current = data.get("current") or data.get("processed") or data.get("done_count") or data.get("move_count")
    try:
        if total is not None and current is not None and int(current) >= int(total) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


async def _wait_115_move_progress(client, move_proid: str, label: str = "移动"):
    move_proid = str(move_proid or "").strip()
    if not move_proid:
        return
    started_at = time.monotonic()
    logger.info(f"[MediaOrganize] 115移动任务已提交: {label} move_proid={move_proid}")
    while True:
        if time.monotonic() - started_at > _MOVE_PROGRESS_TIMEOUT_SECONDS:
            raise TimeoutError(f"{label}移动任务等待超时: move_proid={move_proid}")
        progress = await _run_115_write_request(
            client,
            "查询移动进度",
            lambda write_client: write_client.fs_move_progress({"move_proid": move_proid}, async_=False),
        )
        if _is_115_move_progress_done(progress):
            logger.info(f"[MediaOrganize] 115移动任务完成: {label} move_proid={move_proid}")
            return
        await asyncio.sleep(_MOVE_PROGRESS_POLL_INTERVAL_SECONDS)


def run_115_write_request_sync(
    client,
    request_name: str,
    request_factory: Callable[[Any], Any],
    *,
    raise_on_state_false: bool = True,
):
    global _LAST_WRITE_API_AT

    for attempt in range(_WRITE_API_RATE_LIMIT_MAX_RETRIES + 1):
        with _WRITE_API_RATE_LOCK:
            now = time.monotonic()
            wait_seconds = _WRITE_API_RATE_INTERVAL_SECONDS - (now - _LAST_WRITE_API_AT)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            _LAST_WRITE_API_AT = time.monotonic()

        write_client = _clone_115_client_for_write(client)
        response = request_factory(write_client)

        if isinstance(response, dict) and not response.get("state", True):
            if _is_115_rate_limited_response(response) and attempt < _WRITE_API_RATE_LIMIT_MAX_RETRIES:
                backoff = _get_115_rate_limit_backoff_seconds(attempt)
                logger.warning(
                    f"[115风控(Sync)] {request_name} 触发 990009，退避 {backoff:.1f}s 后重试 "
                    f"({attempt + 1}/{_WRITE_API_RATE_LIMIT_MAX_RETRIES})"
                )
                time.sleep(backoff)
                continue
            if raise_on_state_false:
                raise RuntimeError(f"{request_name}失败: {response}")
        return response


_run_115_write_request_sync = run_115_write_request_sync


async def run_115_write_request(
    client,
    request_name: str,
    request_factory: Callable[[Any], Any],
    *,
    raise_on_state_false: bool = True,
):
    global _LAST_WRITE_API_AT

    for attempt in range(_WRITE_API_RATE_LIMIT_MAX_RETRIES + 1):
        start_at = time.monotonic()
        async with _thread_lock_context(_WRITE_API_RATE_LOCK):
            now = time.monotonic()
            wait_seconds = _WRITE_API_RATE_INTERVAL_SECONDS - (now - _LAST_WRITE_API_AT)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            _LAST_WRITE_API_AT = time.monotonic()

        try:
            write_client = _clone_115_client_for_write(client)
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    _WRITE_REQUEST_EXECUTOR,
                    lambda _write_client=write_client: request_factory(_write_client),
                ),
                timeout=_WRITE_REQUEST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as e:
            elapsed = time.monotonic() - start_at
            logger.warning(f"[115] {request_name}超时: 耗时={elapsed:.2f}s")
            raise TimeoutError(f"{request_name}超时: {elapsed:.2f}s") from e

        if isinstance(result, dict) and not result.get("state", True):
            if _is_115_rate_limited_response(result) and attempt < _WRITE_API_RATE_LIMIT_MAX_RETRIES:
                backoff = _get_115_rate_limit_backoff_seconds(attempt)
                logger.warning(
                    f"[115风控] {request_name} 触发 990009，退避 {backoff:.1f}s 后重试 "
                    f"({attempt + 1}/{_WRITE_API_RATE_LIMIT_MAX_RETRIES})"
                )
                await asyncio.sleep(backoff)
                continue
            if raise_on_state_false:
                raise RuntimeError(f"{request_name}失败: {result}")
        return result


def _ensure_direct_url_worker() -> None:
    global _DIRECT_URL_WORKER_THREAD
    with _DIRECT_URL_WORKER_LOCK:
        if _DIRECT_URL_WORKER_THREAD is not None and _DIRECT_URL_WORKER_THREAD.is_alive():
            return
        _DIRECT_URL_WORKER_THREAD = threading.Thread(
            target=_direct_url_worker,
            name="chillposter-115-direct-url",
            daemon=True,
        )
        _DIRECT_URL_WORKER_THREAD.start()


def _direct_url_queue_has_higher_priority(priority: int) -> bool:
    try:
        next_item = _DIRECT_URL_QUEUE.queue[0] if _DIRECT_URL_QUEUE.queue else None
    except Exception:
        return False
    return bool(next_item and int(next_item[0]) < int(priority))


def _run_request_factory_with_timeout(request_factory: Callable[[], Any]) -> Any:
    result = request_factory()
    if inspect.isawaitable(result):
        return asyncio.run(asyncio.wait_for(result, timeout=_DIRECT_URL_REQUEST_TIMEOUT_SECONDS))
    return result


def _direct_url_worker() -> None:
    global _LAST_DIRECT_URL_AT
    while True:
        priority, sequence, request_name, request_factory, future = _DIRECT_URL_QUEUE.get()
        for attempt in range(_DIRECT_URL_TIMEOUT_MAX_RETRIES + 1):
            try:
                if future.cancelled():
                    break

                if priority > DIRECT_URL_PRIORITY_PLAYBACK:
                    time.sleep(_DIRECT_URL_LOW_PRIORITY_GRACE_SECONDS)
                    if _direct_url_queue_has_higher_priority(priority):
                        _DIRECT_URL_QUEUE.put((priority, sequence, request_name, request_factory, future))
                        break

                now = time.monotonic()
                pacing_seconds = random.uniform(_DIRECT_URL_PACING_MIN_SECONDS, _DIRECT_URL_PACING_MAX_SECONDS)
                wait_seconds = pacing_seconds - (now - _LAST_DIRECT_URL_AT)
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                    if priority > DIRECT_URL_PRIORITY_PLAYBACK and _direct_url_queue_has_higher_priority(priority):
                        _DIRECT_URL_QUEUE.put((priority, sequence, request_name, request_factory, future))
                        break
                _LAST_DIRECT_URL_AT = time.monotonic()

                request_started_at = time.monotonic()
                result = _run_request_factory_with_timeout(request_factory)
                if isinstance(result, dict) and not result.get("state", True):
                    raise RuntimeError(f"{request_name}失败: {result}")
                elapsed = time.monotonic() - request_started_at
                if request_name.startswith("获取直链"):
                    label = request_name.split(":", 1)[1].strip() if ":" in request_name else ""
                    label_text = f": {label}" if label else ""
                    logger.debug(f"[115] 直链获取完成{label_text} | 请求耗时={elapsed:.2f}s")
                if not future.cancelled():
                    future.set_result(result)
                break
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - request_started_at
                if request_name.startswith("获取直链"):
                    label = request_name.split(":", 1)[1].strip() if ":" in request_name else ""
                    label_text = f": {label}" if label else ""
                    request_name_for_log = f"直链获取{label_text}"
                else:
                    request_name_for_log = request_name
                if attempt < _DIRECT_URL_TIMEOUT_MAX_RETRIES:
                    logger.warning(f"[115] {request_name_for_log}超时: 请求耗时={elapsed:.2f}s，准备重试 ({attempt + 1}/{_DIRECT_URL_TIMEOUT_MAX_RETRIES})")
                    continue
                logger.warning(f"[115] {request_name_for_log}超时: 请求耗时={elapsed:.2f}s")
                if not future.cancelled():
                    future.set_result({})
            except Exception as e:
                if not future.cancelled():
                    future.set_exception(e)
                break
        _DIRECT_URL_QUEUE.task_done()


async def _run_115_serial_request(
    request_name: str,
    request_factory: Callable[[], Any],
    *,
    priority: int = DIRECT_URL_PRIORITY_DEFAULT,
):
    _ensure_direct_url_worker()
    future: concurrent.futures.Future = concurrent.futures.Future()
    _DIRECT_URL_QUEUE.put((
        int(priority),
        next(_DIRECT_URL_SEQUENCE),
        request_name,
        request_factory,
        future,
    ))
    return await asyncio.wrap_future(future)


_run_115_write_request = run_115_write_request


def _normalize_115_file_ids(file_ids):
    normalized_ids = file_ids if isinstance(file_ids, list) else int(file_ids)
    return normalized_ids


def _count_115_file_ids(file_ids) -> int:
    return len(file_ids) if isinstance(file_ids, list) else 1


def _log_115_file_op_timing(
    label: str,
    *,
    count: int,
    write_submit_seconds: float,
    move_wait_seconds: float,
    total_seconds: float,
):
    subject = f"{count} 个文件" if count > 1 else "1 个文件"
    message = (
        f"[MediaOrganize] 115文件操作较慢: {label}，{subject}，"
        f"总耗时 {total_seconds:.2f} 秒，提交耗时 {write_submit_seconds:.2f} 秒，"
        f"等待移动完成 {move_wait_seconds:.2f} 秒"
    )
    if (
        total_seconds >= _FILE_OP_TIMING_INFO_THRESHOLD_SECONDS
        or write_submit_seconds >= _FILE_OP_WRITE_SUBMIT_INFO_THRESHOLD_SECONDS
        or move_wait_seconds >= _FILE_OP_MOVE_WAIT_INFO_THRESHOLD_SECONDS
    ):
        logger.info(message)
    else:
        logger.trace(message)


async def _submit_115_move_items(client, file_ids, target_cid: str):
    normalized_ids = _normalize_115_file_ids(file_ids)
    response = await _run_115_write_request(
        client,
        "移动",
        lambda write_client: write_client.fs_move_app(
            normalized_ids,
            pid=int(target_cid),
            app="android",
            async_=False,
        ),
    )
    move_proid = _extract_115_move_progress_id(response)
    count = _count_115_file_ids(normalized_ids)
    return response, move_proid, count


async def _move_115_items(client, file_ids, target_cid: str):
    response, move_proid, count = await _submit_115_move_items(client, file_ids, target_cid)
    if move_proid:
        await _wait_115_move_progress(client, move_proid, label=f"移动{count}项到{target_cid}")
    return response


async def _rename_115_items(client, rename_pairs: list[tuple[int, str]]):
    return await _run_115_write_request(
        client,
        "重命名",
        lambda write_client: write_client.fs_rename_app(
            rename_pairs,
            app="android",
            async_=False,
        ),
    )


async def _rename_115_file(client, file_item: dict, new_name: str, target_cid: str = None, target_path: str = "") -> bool:
    """重命名文件并移动到目标目录"""
    source_path = ""
    try:
        total_started_at = time.monotonic()
        fid = int(file_item.get("fid") or file_item.get("id") or 0)
        if not fid:
            logger.error(f"[MediaOrganize] 文件操作失败: 无法获取文件 ID (file_item={file_item})")
            return False

        old_name = file_item.get('name', '')
        source_path = str(file_item.get('path', '') or old_name)
        display_name = new_name or old_name
        write_submit_seconds = 0.0
        move_wait_seconds = 0.0
        move_proid = ""

        write_submit_started_at = time.monotonic()
        if new_name and new_name != old_name:
            await _rename_115_items(client, [(fid, new_name)])
            logger.info(f"[MediaOrganize] 文件重命名成功: {old_name} -> {new_name}")
        if target_cid:
            _, move_proid, _ = await _submit_115_move_items(client, fid, target_cid)
        write_submit_seconds = time.monotonic() - write_submit_started_at

        if move_proid:
            move_wait_started_at = time.monotonic()
            await _wait_115_move_progress(client, move_proid, label=f"移动1项到{target_cid}")
            move_wait_seconds = time.monotonic() - move_wait_started_at

        if target_cid:
            if target_path:
                final_path = f"{target_path.rstrip('/')}/{display_name}" if display_name else target_path.rstrip('/')
                logger.info(f"[MediaOrganize] 文件移动成功: {source_path} -> {final_path}")
            else:
                logger.info(f"[MediaOrganize] 文件移动成功: {source_path} -> {display_name}")

        _log_115_file_op_timing(
            "单文件",
            count=1,
            write_submit_seconds=write_submit_seconds,
            move_wait_seconds=move_wait_seconds,
            total_seconds=time.monotonic() - total_started_at,
        )
        return True
    except Exception as e:
        logger.error(
            f"[MediaOrganize] 文件操作失败: source={source_path}, new_name={new_name}, "
            f"target={target_path or target_cid or ''}, err={e}"
        )
    return False


async def _rename_115_files_batch(client, file_ops: list[dict], target_cid: str = None, target_path: str = "") -> dict:
    """批量重命名并移动多个文件到同一目标目录。"""
    total_started_at = time.monotonic()
    valid_ops = []
    for item in file_ops or []:
        fid = int(item.get("fid") or item.get("id") or 0)
        if not fid:
            logger.warning(f"[MediaOrganize] 批量文件操作跳过无效ID: {item}")
            continue
        old_name = str(item.get("old_name") or item.get("name") or "")
        new_name = str(item.get("new_name") or old_name)
        source_path = str(item.get("source_path") or item.get("path") or old_name)
        valid_ops.append({
            "fid": fid,
            "old_name": old_name,
            "new_name": new_name,
            "source_path": source_path,
        })

    if not valid_ops:
        return {"ok": False, "items": [], "error": "no_valid_ops"}

    rename_pairs = [
        (op["fid"], op["new_name"])
        for op in valid_ops
        if op["new_name"] and op["new_name"] != op["old_name"]
    ]
    move_ids = [op["fid"] for op in valid_ops]

    rename_done = False
    move_done = False
    write_submit_seconds = 0.0
    move_wait_seconds = 0.0
    move_proid = ""
    move_count = len(move_ids)

    try:
        write_submit_started_at = time.monotonic()
        if rename_pairs:
            await _rename_115_items(client, rename_pairs)
            rename_done = True
            logger.info(f"[MediaOrganize] 批量重命名成功: {len(rename_pairs)} 条")

        if target_cid:
            _, move_proid, move_count = await _submit_115_move_items(client, move_ids, target_cid)
        write_submit_seconds = time.monotonic() - write_submit_started_at

        if move_proid:
            move_wait_started_at = time.monotonic()
            await _wait_115_move_progress(client, move_proid, label=f"移动{move_count}项到{target_cid}")
            move_wait_seconds = time.monotonic() - move_wait_started_at

        if target_cid:
            move_done = True
            logger.info(f"[MediaOrganize] 批量移动成功: {len(move_ids)} 条 -> {target_path or target_cid}")

        for op in valid_ops:
            display_name = op["new_name"] or op["old_name"]
            if op["new_name"] and op["new_name"] != op["old_name"]:
                logger.info(f"[MediaOrganize] 文件重命名成功: {op['old_name']} -> {op['new_name']}")
            if target_cid:
                if target_path:
                    final_path = f"{target_path.rstrip('/')}/{display_name}" if display_name else target_path.rstrip('/')
                    logger.info(f"[MediaOrganize] 文件移动成功: {op['source_path']} -> {final_path}")
                else:
                    logger.info(f"[MediaOrganize] 文件移动成功: {op['source_path']} -> {display_name}")
        _log_115_file_op_timing(
            "批量文件",
            count=len(valid_ops),
            write_submit_seconds=write_submit_seconds,
            move_wait_seconds=move_wait_seconds,
            total_seconds=time.monotonic() - total_started_at,
        )
        return {"ok": True, "items": valid_ops, "error": "", "rename_done": rename_done, "move_done": move_done}
    except Exception as e:
        logger.warning(
            f"[MediaOrganize] 批量文件操作失败，将回退逐条处理: 文件数={len(valid_ops)}, "
            f"target={target_path or target_cid or ''}, rename_done={rename_done}, move_done={move_done}, "
            f"err={type(e).__name__}: {e}"
        )
        return {"ok": False, "items": valid_ops, "error": f"{type(e).__name__}: {e}", "rename_done": rename_done, "move_done": move_done}


_SUBTITLE_TAG_TOKENS = {
    "chs", "cht", "sc", "tc", "eng", "en", "zh", "cn", "jp", "jpn",
    "kr", "kor", "cc", "sdh", "forced", "default", "sub", "subs", "subtitle",
}

_SUBTITLE_TECH_TOKENS = {
    "web", "webdl", "webrip", "bluray", "blu", "ray", "bdrip", "brrip", "remux",
    "hdrip", "dvdrip", "hdtv", "uhd", "sdr", "hdr", "dv", "dovi", "hdr10",
    "hdr10plus", "hlg", "x264", "x265", "h264", "h265", "avc", "hevc", "av1",
    "mpeg2", "mpeg4", "vp9", "vp8", "aac", "ac3", "eac3", "dd", "ddp", "truehd",
    "flac", "dts", "dtshd", "atmos", "ma", "hd", "bit", "proper", "repack",
}


def _normalize_subtitle_tokens(value: str) -> list[str]:
    text = str(value or "").strip().lower()
    if not text:
        return []
    text = re.sub(r"[\[\]\(\){}（）【】]+", " ", text)
    text = re.sub(r"[._\-—–]+", " ", text)
    return [re.sub(r"[^\w一-鿿]+", "", token) for token in re.split(r"\s+", text) if token]


def _is_subtitle_tech_token(token: str) -> bool:
    compact = str(token or "").strip().lower()
    if not compact:
        return False
    if compact in _SUBTITLE_TECH_TOKENS:
        return True
    if re.match(r"^\d{3,4}p$", compact):
        return True
    if re.match(r"^\d{2,3}fps$", compact):
        return True
    if re.match(r"^\d{1,2}bit$", compact):
        return True
    if re.match(r"^[xh]\d{3}$", compact):
        return True
    return False


def _normalize_subtitle_match_text(value: str) -> str:
    tokens = []
    for compact in _normalize_subtitle_tokens(value):
        if not compact or compact in _SUBTITLE_TAG_TOKENS:
            continue
        tokens.append(compact)
    return " ".join(tokens)


def _normalize_subtitle_match_core(value: str) -> str:
    tokens = []
    for compact in _normalize_subtitle_tokens(value):
        if not compact or compact in _SUBTITLE_TAG_TOKENS:
            continue
        if _is_subtitle_tech_token(compact):
            break
        tokens.append(compact)
    return " ".join(tokens)


def _is_subtitle_year_token(token: str) -> bool:
    return bool(re.match(r"^(?:19|20)\d{2}$", str(token or "")))


def _is_subtitle_episode_token(token: str) -> bool:
    compact = str(token or "").strip().lower()
    return bool(re.match(r"^s\d{1,2}e\d{1,4}$", compact) or re.match(r"^(?:ep|e)\d{1,4}$", compact))


def _is_strong_subtitle_title_token(token: str) -> bool:
    compact = str(token or "").strip().lower()
    if not compact or compact.isdigit() or _is_subtitle_year_token(compact) or _is_subtitle_episode_token(compact):
        return False
    return bool(re.search(r"[一-鿿]", compact) or len(compact) >= 3)


def _extract_subtitle_episode_keys(value: str) -> set[str]:
    compact = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    keys = set()
    for season, episode in re.findall(r"s(\d{1,2})\s*e(\d{1,4})", compact):
        keys.add(f"s{int(season):02d}e{int(episode):02d}")
        keys.add(f"e{int(episode):02d}")
    for episode in re.findall(r"(?:^|\s)(?:ep|e)(\d{1,4})(?=\s|$)", compact):
        keys.add(f"e{int(episode):02d}")
    return keys


def _subtitle_cores_match(video_core: str, sub_core: str) -> bool:
    if not video_core or not sub_core:
        return False
    if video_core == sub_core:
        return True

    video_tokens = video_core.split()
    sub_tokens = sub_core.split()
    video_episode_tokens = {t for t in video_tokens if _is_subtitle_episode_token(t)}
    sub_episode_tokens = {t for t in sub_tokens if _is_subtitle_episode_token(t)}
    if video_episode_tokens or sub_episode_tokens:
        if not video_episode_tokens or not sub_episode_tokens or video_episode_tokens.isdisjoint(sub_episode_tokens):
            return False

    video_title_tokens = [t for t in video_tokens if not _is_subtitle_year_token(t) and not _is_subtitle_episode_token(t)]
    sub_title_tokens = [t for t in sub_tokens if not _is_subtitle_year_token(t) and not _is_subtitle_episode_token(t)]
    common = set(video_title_tokens) & set(sub_title_tokens)
    return any(_is_strong_subtitle_title_token(t) for t in common)


def _build_subtitle_suffix(sub_stem: str, video_stem: str) -> str:
    if sub_stem == video_stem or sub_stem.startswith(video_stem + "."):
        return sub_stem[len(video_stem):]
    return ""


def _extract_subtitle_tag_suffix(sub_stem: str) -> str:
    suffix_tokens = []
    for token in reversed(re.split(r"[._\-—–\s]+", str(sub_stem or ""))):
        compact = re.sub(r"[^a-z0-9]+", "", token.lower())
        if compact in _SUBTITLE_TAG_TOKENS:
            suffix_tokens.append(compact)
            continue
        break
    return "." + ".".join(reversed(suffix_tokens)) if suffix_tokens else ""


def _collect_matched_subtitle_ops(file_item: dict, video_new_name: str, subtitles_by_parent: dict,
                                  preserve_subtitle_name: bool = False) -> tuple[str, list, str]:
    parent_id = str(file_item.get("parent_id", ""))
    subs = subtitles_by_parent.get(parent_id, [])
    if not subs:
        return parent_id, [], ""

    video_stem = os.path.splitext(file_item.get("name", ""))[0]
    new_stem = os.path.splitext(video_new_name)[0] if video_new_name else video_stem
    strict_ops = []
    strict_ids = set()

    for sub in subs:
        sub_name = str(sub.get("name", "") or "")
        sub_stem, sub_ext = os.path.splitext(sub_name)
        sub_id = sub.get("id")
        if not sub_id:
            continue
        if sub_stem != video_stem and not sub_stem.startswith(video_stem + "."):
            continue

        if sub_id in strict_ids:
            continue
        strict_ids.add(sub_id)

        strict_ops.append({
            "id": sub_id,
            "fid": sub_id,
            "name": sub_name,
            "old_name": sub_name,
            "new_name": sub_name if preserve_subtitle_name else (new_stem + _build_subtitle_suffix(sub_stem, video_stem) + sub_ext),
            "path": sub.get("path", ""),
            "source_path": sub.get("path", ""),
            "pickcode": sub.get("pickcode", ""),
        })

    if strict_ops:
        return parent_id, strict_ops, "strict"

    video_core = _normalize_subtitle_match_core(video_stem)
    if not video_core:
        return parent_id, [], ""

    loose_ops = []
    loose_ids = set()
    for sub in subs:
        sub_name = str(sub.get("name", "") or "")
        sub_stem, sub_ext = os.path.splitext(sub_name)
        sub_id = sub.get("id")
        if not sub_id or sub_id in loose_ids:
            continue
        sub_core = _normalize_subtitle_match_core(sub_stem)
        core_matched = _subtitle_cores_match(video_core, sub_core)
        if not core_matched:
            video_episode_keys = _extract_subtitle_episode_keys(video_stem)
            sub_episode_keys = _extract_subtitle_episode_keys(sub_stem)
            core_matched = bool(video_episode_keys and sub_episode_keys and not video_episode_keys.isdisjoint(sub_episode_keys))
        if not core_matched:
            continue
        loose_ids.add(sub_id)
        tag_suffix = _extract_subtitle_tag_suffix(sub_stem)
        loose_ops.append({
            "id": sub_id,
            "fid": sub_id,
            "name": sub_name,
            "old_name": sub_name,
            "new_name": sub_name if preserve_subtitle_name else (new_stem + tag_suffix + sub_ext),
            "path": sub.get("path", ""),
            "source_path": sub.get("path", ""),
            "pickcode": sub.get("pickcode", ""),
        })

    if loose_ops:
        return parent_id, loose_ops, "loose_core"
    return parent_id, [], ""


async def _apply_subtitle_move_results(parent_id: str, subs: list, succeeded_ids: set[int], subtitles_by_parent: dict):
    if succeeded_ids:
        subtitles_by_parent[parent_id] = [
            sub for sub in subs
            if int(sub.get("id") or 0) not in succeeded_ids
        ]


async def _move_subtitle_ops(client, parent_id: str, subs: list, matched_ops: list[dict], subtitles_by_parent: dict,
                             target_cid: str, target_path: str = ""):
    moved_subtitles = []
    succeeded_ids = set()

    if len(matched_ops) >= 2:
        batch_result = await _rename_115_files_batch(client, matched_ops, target_cid=target_cid, target_path=target_path)
        if batch_result.get("ok"):
            for op in matched_ops:
                subtitle_id = str(op.get("id") or op.get("fid") or "")
                _record_organized_source_path(subtitle_id, target_path, source_path=op.get("source_path") or op.get("path", ""))
                if subtitle_id.isdigit():
                    succeeded_ids.add(int(subtitle_id))
            await _apply_subtitle_move_results(parent_id, subs, succeeded_ids, subtitles_by_parent)
            return [{"name": op["new_name"], "pickcode": op.get("pickcode", "")} for op in matched_ops]

    for op in matched_ops:
        sub_name = op["old_name"]
        new_sub_name = op["new_name"]
        ok = await _rename_115_file(client, op, new_sub_name, target_cid=target_cid, target_path=target_path)
        if ok:
            subtitle_id = str(op.get("id") or op.get("fid") or "")
            _record_organized_source_path(subtitle_id, target_path, source_path=op.get("source_path") or op.get("path", ""))
            if subtitle_id.isdigit():
                succeeded_ids.add(int(subtitle_id))
            moved_subtitles.append({"name": new_sub_name, "pickcode": op.get("pickcode", "")})
        else:
            logger.warning(f"[MediaOrganize] 字幕移动失败: {sub_name!r}")

    await _apply_subtitle_move_results(parent_id, subs, succeeded_ids, subtitles_by_parent)

    return moved_subtitles


async def _match_and_move_subtitles(client, file_item: dict, video_new_name: str,
                                     subtitles_by_parent: dict, target_cid: str,
                                     target_path: str = ""):
    """查找与视频同目录同名的字幕文件，重命名后移动到目标目录，返回已移动的字幕列表 [(new_name, pickcode)]"""
    parent_id, matched_ops, match_mode = _collect_matched_subtitle_ops(
        file_item,
        video_new_name,
        subtitles_by_parent,
        preserve_subtitle_name=False,
    )
    subs = subtitles_by_parent.get(parent_id, [])
    if not subs:
        return []
    if not matched_ops:
        sub_names = [s.get("name", "") for s in subs]
        logger.info(f"[MediaOrganize] 目录下有字幕但未匹配视频 {file_item.get('name','')!r}: {sub_names}")
        return []

    logger.info(f"[MediaOrganize] 字幕匹配命中: video={file_item.get('name', '')!r} mode={match_mode} 字幕数={len(matched_ops)}")
    for op in matched_ops:
        logger.info(f"[MediaOrganize] 移动字幕: {op['old_name']!r} -> {op['new_name']!r}")

    return await _move_subtitle_ops(
        client,
        parent_id,
        subs,
        matched_ops,
        subtitles_by_parent,
        target_cid=target_cid,
        target_path=target_path,
    )


async def _move_matched_subtitles_to_target(client, file_item: dict, subtitles_by_parent: dict,
                                            target_cid: str, target_path: str = ""):
    parent_id, matched_ops, match_mode = _collect_matched_subtitle_ops(
        file_item,
        str(file_item.get("name", "") or ""),
        subtitles_by_parent,
        preserve_subtitle_name=True,
    )
    subs = subtitles_by_parent.get(parent_id, [])
    if not subs or not matched_ops:
        return []

    logger.info(f"[MediaOrganize] 去重/失败分支字幕联动: video={file_item.get('name', '')!r} mode={match_mode} 字幕数={len(matched_ops)}")
    return await _move_subtitle_ops(
        client,
        parent_id,
        subs,
        matched_ops,
        subtitles_by_parent,
        target_cid=target_cid,
        target_path=target_path,
    )


async def _match_and_move_subtitles_batch(client, subtitle_plans: list[dict], subtitles_by_parent: dict,
                                          target_cid: str, target_path: str = "",
                                          preserve_subtitle_name: bool = False) -> dict[str, list[dict]]:
    grouped_ops: dict[str, list[dict]] = {}
    grouped_subs: dict[str, list] = {}
    result_map: dict[str, list[dict]] = {}
    log_prefix = "去重/失败分支字幕批量联动" if preserve_subtitle_name else "字幕批量匹配命中"

    for plan in subtitle_plans or []:
        file_item = plan.get("file_item") or {}
        video_new_name = str(plan.get("video_new_name", "") or file_item.get("name", "") or "")
        parent_id, matched_ops, match_mode = _collect_matched_subtitle_ops(
            file_item,
            video_new_name,
            subtitles_by_parent,
            preserve_subtitle_name=preserve_subtitle_name,
        )
        subs = subtitles_by_parent.get(parent_id, [])
        video_id = str(plan.get("video_id") or file_item.get("id") or file_item.get("fid") or "")
        result_map[video_id] = []
        if not subs:
            continue
        if not matched_ops:
            sub_names = [s.get("name", "") for s in subs]
            logger.info(f"[MediaOrganize] 目录下有字幕但未匹配视频 {file_item.get('name','')!r}: {sub_names}")
            continue

        logger.info(f"[MediaOrganize] {log_prefix}: video={file_item.get('name', '')!r} mode={match_mode} 字幕数={len(matched_ops)}")
        for op in matched_ops:
            op["video_id"] = video_id
            grouped_ops.setdefault(parent_id, []).append(op)
            logger.info(f"[MediaOrganize] 批量移动字幕: {op['old_name']!r} -> {op['new_name']!r}")
        grouped_subs[parent_id] = subs

    for parent_id, ops in grouped_ops.items():
        if not ops:
            continue
        subs = grouped_subs.get(parent_id, [])
        unique_ops = []
        seen_ids = set()
        for op in ops:
            subtitle_id = str(op.get("id") or op.get("fid") or "")
            if not subtitle_id or subtitle_id in seen_ids:
                continue
            seen_ids.add(subtitle_id)
            unique_ops.append(op)
        if not unique_ops:
            continue

        batch_result = await _rename_115_files_batch(client, unique_ops, target_cid=target_cid, target_path=target_path)
        if batch_result.get("ok"):
            succeeded_ids = set()
            for op in unique_ops:
                subtitle_id = str(op.get("id") or op.get("fid") or "")
                _record_organized_source_path(subtitle_id, target_path, source_path=op.get("source_path") or op.get("path", ""))
                if subtitle_id.isdigit():
                    succeeded_ids.add(int(subtitle_id))
                video_id = str(op.get("video_id") or "")
                if video_id:
                    result_map.setdefault(video_id, []).append({"name": op["new_name"], "pickcode": op.get("pickcode", "")})
            await _apply_subtitle_move_results(parent_id, subs, succeeded_ids, subtitles_by_parent)
            continue

        rename_done = bool(batch_result.get("rename_done"))
        move_done = bool(batch_result.get("move_done"))
        logger.warning(
            f"[MediaOrganize] 字幕批量处理失败，回退逐条处理: parent_id={parent_id}, "
            f"rename_done={rename_done}, move_done={move_done}, err={batch_result.get('error', '')}"
        )
        for op in unique_ops:
            fallback_op = dict(op)
            fallback_op["name"] = op["new_name"] if rename_done else op["old_name"]
            fallback_name = "" if rename_done else op["new_name"]
            fallback_target_cid = None if move_done else target_cid
            ok = await _rename_115_file(
                client,
                fallback_op,
                fallback_name,
                target_cid=fallback_target_cid,
                target_path=target_path,
            )
            if not ok:
                logger.warning(f"[MediaOrganize] 字幕移动失败: {op['old_name']!r}")
                continue
            subtitle_id = str(op.get("id") or op.get("fid") or "")
            _record_organized_source_path(subtitle_id, target_path, source_path=op.get("source_path") or op.get("path", ""))
            succeeded_ids = {int(subtitle_id)} if subtitle_id.isdigit() else set()
            await _apply_subtitle_move_results(parent_id, subtitles_by_parent.get(parent_id, subs), succeeded_ids, subtitles_by_parent)
            video_id = str(op.get("video_id") or "")
            if video_id:
                result_map.setdefault(video_id, []).append({"name": op["new_name"], "pickcode": op.get("pickcode", "")})

    return result_map


def _resolve_failed_move_target(client, file_item: dict, source_cid_str: str) -> tuple[str, bool, str]:
    """返回要移动到失败目录的 ID：源目录根文件移动文件本身，子目录文件移动源目录下顶层目录。"""
    file_id = str(file_item.get("id") or file_item.get("fid", ""))
    file_name = str(file_item.get("name", "") or "")
    source_cid = str(source_cid_str or "")
    parent_id = str(file_item.get("parent_id", "") or "")
    if not file_id:
        return "", False, file_name
    if source_cid and parent_id == source_cid:
        return file_id, False, file_name

    ancestors = file_item.get("ancestors") or []
    if isinstance(ancestors, list) and source_cid:
        source_seen = False
        for ancestor in ancestors:
            if not isinstance(ancestor, dict):
                continue
            ancestor_id = str(
                ancestor.get("id")
                or ancestor.get("cid")
                or ancestor.get("category_id")
                or ""
            )
            ancestor_name = str(ancestor.get("name", "") or "")
            if not ancestor_id:
                continue
            if source_seen:
                return ancestor_id, True, ancestor_name or file_name
            if ancestor_id == source_cid:
                source_seen = True

    if not source_cid:
        return file_id, False, file_name

    try:
        fs = _get_115_fs(client)
        current_id = file_id
        current_name = file_name
        seen_ids = set()
        for _ in range(30):
            if not current_id or current_id in seen_ids:
                break
            seen_ids.add(current_id)
            with _read_lock:
                attr = fs._get_attr_by_id(int(current_id))
            current_name = str(attr.get("name", "") or current_name)
            current_parent = str(attr.get("parent_id", attr.get("cid", "")) or "")
            if current_parent == source_cid:
                return current_id, current_id != file_id, current_name
            current_id = current_parent
    except Exception as e:
        logger.debug(f"[MediaOrganize] 解析失败移动顶层目录失败，回退移动文件: {file_name}, err={e}")

    return file_id, False, file_name


async def _move_top_dir_to_failed(client, file_item: dict, source_cid_str: str,
                                  failed_dir_cid: str, moved_dirs: set,
                                  subtitles_by_parent: dict | None = None,
                                  target_path: str = "",
                                  move_top_dir: bool = True):
    """将失败文件所在的源目录顶层文件夹移到失败目录；根目录文件则移动文件及匹配字幕。"""
    try:
        file_id = str(file_item.get("id") or file_item.get("fid", ""))
        if move_top_dir:
            target_id, moving_dir, target_name = _resolve_failed_move_target(client, file_item, source_cid_str)
        else:
            target_id = file_id
            moving_dir = False
            target_name = str(file_item.get("name", "") or "")
        if target_id and target_id not in moved_dirs:
            await _move_115_items(client, int(target_id), failed_dir_cid)
            moved_dirs.add(target_id)
            if file_id:
                moved_dirs.add(file_id)
            if target_path:
                _record_organized_source_path(file_id or target_id, target_path, source_path=file_item.get("path", ""))
            label = "目录" if moving_dir else "文件"
            logger.debug(f"[MediaOrganize] 移动失败{label}: {target_name or file_item.get('name', '')} -> 整理失败目录")
        if subtitles_by_parent and not moving_dir:
            await _move_matched_subtitles_to_target(
                client,
                file_item,
                subtitles_by_parent,
                target_cid=str(failed_dir_cid),
                target_path=target_path,
            )
    except Exception as e:
        logger.warning(
            f"[MediaOrganize] 移动失败文件失败: file={file_item.get('name', '')}, "
            f"source_cid={source_cid_str}, target={target_path or failed_dir_cid}, err={e}"
        )


async def _move_failed_files_batch(client, group_failed: list, source_cid: str,
                                   failed_dir_cid: str, moved_dirs: set,
                                   subtitles_by_parent: dict | None = None):
    """批量将失败文件所在的源目录顶层文件夹移到失败目录，一次 API 请求。"""
    ids_to_move = []
    seen_targets = set()
    file_ids_by_target = {}
    moving_dir_by_target = {}
    direct_file_ids = set()
    for fi in group_failed:
        file_id = str(fi.get("id") or fi.get("fid", ""))
        target_id, moving_dir, target_name = _resolve_failed_move_target(client, fi, source_cid)
        if target_id and file_id:
            file_ids_by_target.setdefault(target_id, []).append(file_id)
        if target_id and file_id and not moving_dir:
            direct_file_ids.add(file_id)
        if target_id and target_id not in moved_dirs and target_id not in seen_targets:
            seen_targets.add(target_id)
            ids_to_move.append((target_id, target_name or fi.get("name", "")))
            moving_dir_by_target[target_id] = moving_dir

    if not ids_to_move:
        return

    try:
        await _move_115_items(client, [int(fid) for fid, _ in ids_to_move], failed_dir_cid)
        for target_id, name in ids_to_move:
            moved_dirs.add(target_id)
            for source_file_id in file_ids_by_target.get(target_id, []):
                moved_dirs.add(source_file_id)
            label = "目录" if moving_dir_by_target.get(target_id) else "文件"
            logger.debug(f"[MediaOrganize] 移动失败{label}: {name} -> 整理失败目录")
    except Exception as e:
        logger.warning(f"[MediaOrganize] 批量移动失败文件/目录失败: 条目数={len(ids_to_move)}, err={e}")
        # 降级逐个移动
        for fi in group_failed:
            await _move_top_dir_to_failed(client, fi, source_cid, failed_dir_cid, moved_dirs,
                                          subtitles_by_parent=subtitles_by_parent)
        return

    if subtitles_by_parent:
        for fi in group_failed:
            file_id = str(fi.get("id") or fi.get("fid", ""))
            if file_id in direct_file_ids and file_id in moved_dirs:
                await _move_matched_subtitles_to_target(
                    client, fi, subtitles_by_parent,
                    target_cid=str(failed_dir_cid), target_path="",
                )


def _upload_file_to_115(client, local_path: str, target_cid: str, skip_move_check: bool = False) -> bool:
    """将本地文件上传到 115 目标目录

    使用 P115MultipartUpload.from_path 完成上传：
    - 返回 dict → 秒传成功
    - 返回 P115MultipartUpload → 需要实际上传，iter_upload + complete
    秒传模式下 115 可能将文件放到已存在同名文件的目录而非 pid 指定的目录，
    所以上传后需要检查文件是否在正确位置，不在则移动过去。
    """
    try:
        from p115client.tool.upload import P115MultipartUpload

        filename = os.path.basename(local_path)

        result = P115MultipartUpload.from_path(
            local_path,
            pid=int(target_cid),
            filename=filename,
            user_id=client.user_id,
            user_key=client.user_key,
            async_=False,
        )

        # 秒传成功：from_path 直接返回 dict
        if isinstance(result, dict):
            reused = result.get("reuse")
            file_id = None
            data = result.get("data", {})
            if isinstance(data, dict):
                file_id = data.get("file_id") or data.get("id")
            if not skip_move_check:
                _check_and_move(client, file_id, target_cid, filename, reused=True)
            return True

        # 需要实际上传：返回的是 P115MultipartUpload 对象
        uploader = result
        for _ in uploader.iter_upload(async_=False):
            pass
        complete_result = uploader.complete(async_=False)

        if complete_result and complete_result.get("state"):
            file_id = None
            data = complete_result.get("data", {})
            if isinstance(data, dict):
                file_id = data.get("file_id") or data.get("id")
            if not skip_move_check:
                _check_and_move(client, file_id, target_cid, filename, reused=False)
            return True
        else:
            error = complete_result.get("error", "未知错误") if complete_result else "无响应"
            logger.error(f"[MediaOrganize] 上传失败 {filename}: {error}")
    except Exception as e:
        logger.error(f"[MediaOrganize] 上传异常 {local_path}: {e}")
    return False


def _check_and_move(client, file_id, target_cid: str, filename: str, reused: bool):
    """检查文件是否在目标目录，不在则移动过去"""
    tag = "秒传" if reused else "上传"
    if file_id:
        try:
            fs = _get_115_fs(client)
            attr = fs._get_attr_by_id(int(file_id))
            actual_parent = str(attr.get("parent_id", ""))
            if actual_parent != str(target_cid):
                run_115_write_request_sync(
                    client,
                    f"{tag}后移动文件",
                    lambda write_client: _get_115_fs(write_client).move(int(file_id), to_dir=int(target_cid)),
                )
                logger.debug(f"[MediaOrganize] {tag}成功: {filename} (已移动到目标目录)")
            else:
                logger.debug(f"[MediaOrganize] {tag}成功: {filename}")
        except Exception as e:
            logger.warning(f"[MediaOrganize] 上传后移动文件失败 {filename}: {e}")
    else:
        logger.debug(f"[MediaOrganize] {tag}成功: {filename}")


# ==========================================
# 视频 / 字幕文件扫描
# ==========================================

def _list_115_tree_entries(client, cid: str) -> list[dict]:
    """用 traverse_tree_with_path 递归列出源目录树。"""
    from p115client.tool.iterdir import traverse_tree_with_path

    global _LAST_TREE_SCAN_FINISHED_AT
    with _TREE_SCAN_LOCK:
        elapsed_since_last_scan = time.monotonic() - _LAST_TREE_SCAN_FINISHED_AT
        if elapsed_since_last_scan < _TREE_SCAN_MIN_INTERVAL_SECONDS:
            time.sleep(_TREE_SCAN_MIN_INTERVAL_SECONDS - elapsed_since_last_scan)
        scan_started_at = time.monotonic()
        _prime_115_pickcode_stable_point(client, str(cid))
        with _read_lock:
            items = list(traverse_tree_with_path(
                client,
                cid=int(cid),
                with_ancestors=True,
                app="android",
                max_workers=0,
            ))
        _LAST_TREE_SCAN_FINISHED_AT = time.monotonic()
        logger.debug(f"[MediaOrganize] 目录树遍历完成: cid={cid} 条目={len(items)} 耗时={_LAST_TREE_SCAN_FINISHED_AT - scan_started_at:.2f}s")
        return items



def _iter_115_media_entries_from_tree(items: list[dict]) -> Iterator[dict]:
    for item in items or []:
        if item.get("is_dir"):
            continue
        name = str(item.get("name", "") or "")
        ext = os.path.splitext(name)[1].lower()
        item_id = item.get("id")
        parent_id = item.get("parent_id", item.get("cid", ""))
        pickcode = item.get("pickcode", item.get("pick_code", ""))
        if ext in VIDEO_EXTS:
            yield {
                "kind": "video",
                "item": {
                    "name": name,
                    "id": item_id,
                    "pickcode": pickcode,
                    "parent_id": parent_id,
                    "size": item.get("size", 0),
                    "path": item.get("path", ""),
                    "ancestors": item.get("ancestors", []),
                    "sha1": str(item.get("sha1", "") or "").upper(),
                },
            }
        elif ext in SUBTITLE_EXTS:
            yield {
                "kind": "subtitle",
                "item": {
                    "name": name,
                    "id": item_id,
                    "pickcode": pickcode,
                    "parent_id": parent_id,
                    "path": item.get("path", ""),
                    "ancestors": item.get("ancestors", []),
                },
            }



def _iter_115_media_entries(client, cid: str) -> Iterator[dict]:
    """用源目录树快照产出视频/字幕条目，不写媒体库缓存。"""
    yield from _iter_115_media_entries_from_tree(_list_115_tree_entries(client, cid))


def _list_115_video_files(client, cid: str) -> tuple:
    """用 traverse_tree_with_path 递归列出 115 目录下所有视频文件和字幕文件"""
    try:
        files = []
        subtitles_by_parent = {}  # parent_id -> [{name, id, parent_id, path}]
        for entry in _iter_115_media_entries(client, cid):
            kind = entry.get("kind")
            item = entry.get("item") or {}
            if kind == "video":
                files.append(item)
            elif kind == "subtitle":
                parent_id = item.get("parent_id")
                subtitles_by_parent.setdefault(str(parent_id), []).append(item)
        sub_count = sum(len(v) for v in subtitles_by_parent.values())
        logger.debug(f"[MediaOrganize] 扫描到 {len(files)} 个视频文件, {sub_count} 个字幕文件")
        return files, subtitles_by_parent
    except Exception as e:
        logger.error(f"[MediaOrganize] 列出视频文件失败: {e}")
        return [], {}


# ==========================================
# 直链获取
# ==========================================

async def _get_115_direct_urls(pickcodes, drive_index: int = 0) -> dict[str, str]:
    """批量通过 pickcode 获取 115 文件直链，供 ffprobe 探测用"""
    try:
        from app.services.drive115_service import drive115_service
        return await drive115_service._download_urls_via_client(
            pickcodes,
            user_agent="Mozilla/5.0",
            emby_index=drive_index,
        )
    except Exception as e:
        logger.debug(f"[MediaOrganize] 批量获取直链失败: {e}")
        return {}


async def _get_115_direct_url(pickcode: str, drive_index: int = 0) -> Optional[str]:
    """通过 pickcode 获取 115 文件直链，供 ffprobe 探测用"""
    urls = await _get_115_direct_urls([pickcode], drive_index)
    return urls.get(str(pickcode or "").strip())


# ==========================================
# 媒体库缓存辅助
# ==========================================

def _collect_event_video_sha1s_for_cache(file_id: str, file_name: str, drive_index: int, raw_event: dict = None) -> set:
    """从单个 life 事件中尽可能提取视频 sha1（文件/文件夹移动与删除）"""
    raw = raw_event or {}
    sha1_set = set()

    raw_sha1 = str(raw.get("sha1", "") or "").upper().strip()
    # 仅视频文件的 SHA1 有意义，字幕等其他文件不应写入视频去重缓存
    ext = os.path.splitext(str(file_name or ""))[1].lower()
    if raw_sha1 and ext in VIDEO_EXTS:
        sha1_set.add(raw_sha1)

    file_id_str = str(file_id or "")
    if not file_id_str.isdigit():
        return sha1_set

    is_dir = bool(raw.get("file_category") == 0) or (not raw_sha1 and not str(raw.get("ico", "") or "").strip())

    try:
        client = _get_115_client(drive_index)
        from p115client.tool.attr import get_attr

        if not is_dir and not sha1_set:
            with _read_lock:
                attr = get_attr(client, int(file_id_str))
            attr_sha1 = str((attr or {}).get("sha1") or "").upper().strip()
            if attr_sha1:
                sha1_set.add(attr_sha1)
            return sha1_set

        if is_dir:
            from p115client.tool.iterdir import iter_files_with_path
            with _read_lock:
                items = list(iter_files_with_path(client, cid=int(file_id_str), app="android", cooldown=1.0, page_size=1000))
            for item in items:
                if item.get("is_dir"):
                    continue
                name = str(item.get("name", "") or "")
                if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
                    continue
                s = str(item.get("sha1", "") or "").upper().strip()
                if not s:
                    try:
                        with _read_lock:
                            attr = get_attr(client, int(item.get("id", 0) or 0))
                        s = str((attr or {}).get("sha1") or "").upper().strip()
                    except Exception:
                        s = ""
                if s:
                    sha1_set.add(s)
    except Exception as e:
        logger.debug(f"[MediaOrganize] 提取事件SHA1失败: file={file_name}, file_id={file_id_str}, err={e}")

    return sha1_set
