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
from contextlib import asynccontextmanager
from typing import Optional, Iterator, Callable, Any

from core.logger import logger
from core.media_library_cache import get_task_items, upsert_dir_item
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
    parent_match = None
    items = get_task_items(task_key)
    for item in items.values():
        if not item.get("is_dir"):
            continue
        if normalized_path and str(item.get("path", "") or "").rstrip("/") == normalized_path:
            return str(item.get("id", 0) or ""), str(item.get("pickcode", "") or ""), "path"
        if parent_match is None and item.get("parent_id") == parent_id and item.get("name") == name:
            parent_match = item
    if parent_match:
        return str(parent_match.get("id", 0) or ""), str(parent_match.get("pickcode", "") or ""), "parent+name"
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
                logger.debug(f"[CategoryDir] 创建前命中缓存(path): path={normalized_dir_path}, cid={cid}")
            else:
                logger.debug(f"[CategoryDir] 创建前命中缓存(parent+name): parent={parent_cid}, name={name}, cid={cid}")
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
            logger.debug(f"[CategoryDir] 目录链命中任务缓存: {normalized_category_path} (cid={cached_cid})")
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

_WRITE_API_PACING_MIN_SECONDS = 1.0
_WRITE_API_PACING_MAX_SECONDS = 2.0
_DIRECT_URL_PACING_MIN_SECONDS = 1.0
_DIRECT_URL_PACING_MAX_SECONDS = 2.0
_WRITE_API_LOCK = threading.Lock()
_LAST_WRITE_API_AT = 0.0
_DIRECT_URL_LOCK = threading.Lock()
_LAST_DIRECT_URL_AT = 0.0
_RENAME_LOCK = threading.Lock()
_DIRECT_URL_REQUEST_TIMEOUT_SECONDS = 10
_DIRECT_URL_TIMEOUT_MAX_RETRIES = 1
_WRITE_REQUEST_TIMEOUT_SECONDS = 20
_WRITE_API_RATE_LIMIT_MAX_RETRIES = 3
_WRITE_API_RATE_LIMIT_BASE_BACKOFF_SECONDS = 3.0
_115_WAF_COOLDOWN_SECONDS = 1800
_115_COOLDOWN_UNTIL = 0.0
_115_COOLDOWN_LOCK = threading.Lock()
_TREE_SCAN_MIN_INTERVAL_SECONDS = 5.0
_TREE_SCAN_LOCK = threading.Lock()
_LAST_TREE_SCAN_FINISHED_AT = 0.0


@asynccontextmanager
async def _thread_lock_context(lock: threading.Lock):
    await asyncio.to_thread(lock.acquire)
    try:
        yield
    finally:
        lock.release()


class OneOneFiveWafBlockedError(RuntimeError):
    def __init__(self, operation: str, cooldown_seconds: int, detail: str = ""):
        self.operation = operation
        self.cooldown_seconds = max(0, int(cooldown_seconds or 0))
        self.detail = str(detail or "")[:500]
        message = f"115 风控冷却中: operation={operation}, remaining={self.cooldown_seconds}s"
        if self.detail:
            message = f"{message}, detail={self.detail}"
        super().__init__(message)


def _clone_115_client_for_write(client):
    from p115client import P115Client
    return P115Client(str(client.cookies_str), app="android")


def _normalize_numeric_setting(value: Any, default: float, minimum: float = 0.1) -> float:
    try:
        return max(minimum, float(value))
    except Exception:
        return default


def _configure_115_op_tuning(config_data: dict | None):
    global _WRITE_API_PACING_MIN_SECONDS, _WRITE_API_PACING_MAX_SECONDS
    global _DIRECT_URL_PACING_MIN_SECONDS, _DIRECT_URL_PACING_MAX_SECONDS, _115_WAF_COOLDOWN_SECONDS
    data = config_data if isinstance(config_data, dict) else {}
    write_min = _normalize_numeric_setting(data.get("write_pacing_min_seconds"), 1.0)
    write_max = _normalize_numeric_setting(data.get("write_pacing_max_seconds"), 2.0)
    direct_min = _normalize_numeric_setting(data.get("direct_link_pacing_min_seconds"), 1.0)
    direct_max = _normalize_numeric_setting(data.get("direct_link_pacing_max_seconds"), 2.0)
    try:
        cooldown = max(60, int(data.get("waf_cooldown_seconds") or 1800))
    except Exception:
        cooldown = 1800
    _WRITE_API_PACING_MIN_SECONDS = write_min
    _WRITE_API_PACING_MAX_SECONDS = max(write_min, write_max)
    _DIRECT_URL_PACING_MIN_SECONDS = direct_min
    _DIRECT_URL_PACING_MAX_SECONDS = max(direct_min, direct_max)
    _115_WAF_COOLDOWN_SECONDS = cooldown


def _stringify_115_error(value: Any, depth: int = 0) -> str:
    if depth > 3:
        return ""
    parts = []
    if isinstance(value, dict):
        for key, item in value.items():
            parts.append(str(key))
            parts.append(_stringify_115_error(item, depth + 1))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            parts.append(_stringify_115_error(item, depth + 1))
    else:
        parts.append(str(value or ""))
    return " ".join(part for part in parts if part)[:5000]


def _is_115_rate_limited_response(response: Any) -> bool:
    text = _stringify_115_error(response)
    return (
        (isinstance(response, dict) and (
            str(response.get("code", "")) == "990009"
            or str(response.get("errno", "")) == "990009"
            or str(response.get("errNo", "")) == "990009"
        ))
        or "990009" in text
        or "操作频繁" in text
        or "操作尚未执行完成" in text
    )


def _is_115_waf_blocked(value: Any) -> bool:
    text = _stringify_115_error(value).lower()
    if not text:
        return False
    return any(token in text for token in (
        "response [403]",
        "response [405]",
        "response [429]",
        "code=403",
        "code=405",
        "code=429",
        "httpstatuserror",
        "method not allowed",
        "too many requests",
        "your request has been blocked",
        "potential threats to the server's security",
        "errors.aliyun.com",
        "tengine",
        "block_message",
        "访问被阻断",
        "安全威胁",
        "访问受限",
        "验证码",
        "captcha",
        "forbidden",
        "waf",
    ))


def _get_115_cooldown_remaining_seconds() -> int:
    with _115_COOLDOWN_LOCK:
        return max(0, int(_115_COOLDOWN_UNTIL - time.monotonic()))


def _raise_if_115_cooldown_active(operation: str):
    remaining = _get_115_cooldown_remaining_seconds()
    if remaining > 0:
        raise OneOneFiveWafBlockedError(operation, remaining, "cooldown_active")


def _trip_115_cooldown(operation: str, err_or_resp: Any):
    global _115_COOLDOWN_UNTIL
    detail = _stringify_115_error(err_or_resp)
    with _115_COOLDOWN_LOCK:
        _115_COOLDOWN_UNTIL = max(_115_COOLDOWN_UNTIL, time.monotonic() + _115_WAF_COOLDOWN_SECONDS)
        remaining = max(0, int(_115_COOLDOWN_UNTIL - time.monotonic()))
    logger.warning(f"[115风控] {operation} 触发 WAF/封控，进入冷却 {remaining}s: {detail[:300]}")
    raise OneOneFiveWafBlockedError(operation, remaining, detail)


def _get_115_rate_limit_backoff_seconds(attempt: int) -> float:
    return _WRITE_API_RATE_LIMIT_BASE_BACKOFF_SECONDS * (2 ** attempt)


def run_115_write_request_sync(
    client,
    request_name: str,
    request_factory: Callable[[Any], Any],
    *,
    raise_on_state_false: bool = True,
):
    global _LAST_WRITE_API_AT

    for attempt in range(_WRITE_API_RATE_LIMIT_MAX_RETRIES + 1):
        _raise_if_115_cooldown_active(request_name)
        with _WRITE_API_LOCK:
            now = time.monotonic()
            pacing_seconds = random.uniform(_WRITE_API_PACING_MIN_SECONDS, _WRITE_API_PACING_MAX_SECONDS)
            wait_seconds = pacing_seconds - (now - _LAST_WRITE_API_AT)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            _LAST_WRITE_API_AT = time.monotonic()

            write_client = _clone_115_client_for_write(client)
            try:
                response = request_factory(write_client)
            except OneOneFiveWafBlockedError:
                raise
            except Exception as e:
                if _is_115_waf_blocked(e):
                    _trip_115_cooldown(request_name, e)
                raise

        if _is_115_waf_blocked(response):
            _trip_115_cooldown(request_name, response)
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


async def run_115_write_request(client, request_name: str, request_factory: Callable[[Any], Any]):
    global _LAST_WRITE_API_AT

    for attempt in range(_WRITE_API_RATE_LIMIT_MAX_RETRIES + 1):
        _raise_if_115_cooldown_active(request_name)
        start_at = time.monotonic()
        async with _thread_lock_context(_WRITE_API_LOCK):
            try:
                now = time.monotonic()
                pacing_seconds = random.uniform(_WRITE_API_PACING_MIN_SECONDS, _WRITE_API_PACING_MAX_SECONDS)
                wait_seconds = pacing_seconds - (now - _LAST_WRITE_API_AT)
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                _LAST_WRITE_API_AT = time.monotonic()

                write_client = _clone_115_client_for_write(client)
                result = await asyncio.wait_for(
                    asyncio.to_thread(request_factory, write_client),
                    timeout=_WRITE_REQUEST_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as e:
                elapsed = time.monotonic() - start_at
                logger.warning(f"[115] {request_name}超时: 耗时={elapsed:.2f}s")
                raise TimeoutError(f"{request_name}超时: {elapsed:.2f}s") from e
            except OneOneFiveWafBlockedError:
                raise
            except Exception as e:
                if _is_115_waf_blocked(e):
                    _trip_115_cooldown(request_name, e)
                raise

        if _is_115_waf_blocked(result):
            _trip_115_cooldown(request_name, result)
        if isinstance(result, dict) and not result.get("state", True):
            if _is_115_rate_limited_response(result) and attempt < _WRITE_API_RATE_LIMIT_MAX_RETRIES:
                backoff = _get_115_rate_limit_backoff_seconds(attempt)
                logger.warning(
                    f"[115风控] {request_name} 触发 990009，退避 {backoff:.1f}s 后重试 "
                    f"({attempt + 1}/{_WRITE_API_RATE_LIMIT_MAX_RETRIES})"
                )
                await asyncio.sleep(backoff)
                continue
            raise RuntimeError(f"{request_name}失败: {result}")
        return result


async def _run_115_serial_request(request_name: str, request_factory: Callable[[], Any]):
    global _LAST_DIRECT_URL_AT
    _raise_if_115_cooldown_active(request_name)
    await asyncio.to_thread(_DIRECT_URL_LOCK.acquire)
    try:
        for attempt in range(_DIRECT_URL_TIMEOUT_MAX_RETRIES + 1):
            _raise_if_115_cooldown_active(request_name)
            now = time.monotonic()
            pacing_seconds = random.uniform(_DIRECT_URL_PACING_MIN_SECONDS, _DIRECT_URL_PACING_MAX_SECONDS)
            wait_seconds = pacing_seconds - (now - _LAST_DIRECT_URL_AT)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            _LAST_DIRECT_URL_AT = time.monotonic()

            request_started_at = time.monotonic()
            try:
                result = request_factory()
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=_DIRECT_URL_REQUEST_TIMEOUT_SECONDS)
                if _is_115_waf_blocked(result):
                    _trip_115_cooldown(request_name, result)
                if isinstance(result, dict) and not result.get("state", True):
                    raise RuntimeError(f"{request_name}失败: {result}")
                elapsed = time.monotonic() - request_started_at
                if request_name == "获取直链":
                    logger.debug(f"[115] {request_name}完成: 请求耗时={elapsed:.2f}s")
                return result
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - request_started_at
                if attempt < _DIRECT_URL_TIMEOUT_MAX_RETRIES:
                    logger.warning(f"[115] {request_name}超时: 请求耗时={elapsed:.2f}s，准备重试 ({attempt + 1}/{_DIRECT_URL_TIMEOUT_MAX_RETRIES})")
                    continue
                logger.warning(f"[115] {request_name}超时: 请求耗时={elapsed:.2f}s")
                return {}
            except OneOneFiveWafBlockedError:
                raise
            except Exception as e:
                if _is_115_waf_blocked(e):
                    _trip_115_cooldown(request_name, e)
                raise
    finally:
        _DIRECT_URL_LOCK.release()


_run_115_write_request = run_115_write_request


async def _move_115_items(client, file_ids, target_cid: str):
    normalized_ids = file_ids if isinstance(file_ids, list) else int(file_ids)
    return await _run_115_write_request(
        client,
        "移动",
        lambda write_client: write_client.fs_move_app(
            normalized_ids,
            pid=int(target_cid),
            app="android",
            async_=False,
        ),
    )


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
    try:
        fid = int(file_item.get("fid") or file_item.get("id") or 0)
        if not fid:
            logger.error(f"[MediaOrganize] 文件操作失败: 无法获取文件 ID (file_item={file_item})")
            return False

        old_name = file_item.get('name', '')
        source_path = str(file_item.get('path', '') or old_name)
        display_name = new_name or old_name

        async with _thread_lock_context(_RENAME_LOCK):
            if new_name and new_name != old_name:
                await _rename_115_items(client, [(fid, new_name)])
                logger.info(f"[MediaOrganize] 文件重命名成功: {old_name} -> {new_name}")
            if target_cid:
                await _move_115_items(client, fid, target_cid)
                if target_path:
                    final_path = f"{target_path.rstrip('/')}/{display_name}" if display_name else target_path.rstrip('/')
                    logger.info(f"[MediaOrganize] 文件移动成功: {source_path} -> {final_path}")
                else:
                    logger.info(f"[MediaOrganize] 文件移动成功: {source_path} -> {display_name}")
        return True
    except OneOneFiveWafBlockedError:
        raise
    except Exception as e:
        logger.error(
            f"[MediaOrganize] 文件操作失败: source={source_path}, new_name={new_name}, "
            f"target={target_path or target_cid or ''}, err={e}"
        )
    return False


async def _rename_115_files_batch(client, file_ops: list[dict], target_cid: str = None, target_path: str = "") -> dict:
    """批量重命名并移动多个文件到同一目标目录。"""
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

    try:
        async with _thread_lock_context(_RENAME_LOCK):
            if rename_pairs:
                await _rename_115_items(client, rename_pairs)
                rename_done = True
                logger.info(f"[MediaOrganize] 批量重命名成功: {len(rename_pairs)} 条")

            if target_cid:
                await _move_115_items(client, move_ids, target_cid)
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
        return {"ok": True, "items": valid_ops, "error": "", "rename_done": rename_done, "move_done": move_done}
    except OneOneFiveWafBlockedError as e:
        logger.warning(
            f"[MediaOrganize] 批量文件操作触发115风控，停止回退逐条处理: count={len(valid_ops)}, "
            f"target={target_path or target_cid or ''}, rename_done={rename_done}, move_done={move_done}, err={e}"
        )
        return {"ok": False, "blocked": True, "items": valid_ops, "error": str(e), "rename_done": rename_done, "move_done": move_done}
    except Exception as e:
        logger.warning(
            f"[MediaOrganize] 批量文件操作失败，将回退逐条处理: count={len(valid_ops)}, "
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
        if not _subtitle_cores_match(video_core, sub_core):
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
                _record_organized_source_path(subtitle_id, target_path)
                if subtitle_id.isdigit():
                    succeeded_ids.add(int(subtitle_id))
            await _apply_subtitle_move_results(parent_id, subs, succeeded_ids, subtitles_by_parent)
            return [{"name": op["new_name"], "pickcode": op.get("pickcode", "")} for op in matched_ops]
        if batch_result.get("blocked"):
            raise OneOneFiveWafBlockedError("字幕批量移动", _get_115_cooldown_remaining_seconds(), batch_result.get("error", ""))

    for op in matched_ops:
        sub_name = op["old_name"]
        new_sub_name = op["new_name"]
        ok = await _rename_115_file(client, op, new_sub_name, target_cid=target_cid, target_path=target_path)
        if ok:
            subtitle_id = str(op.get("id") or op.get("fid") or "")
            _record_organized_source_path(subtitle_id, target_path)
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
        logger.debug(f"[MediaOrganize] 目录下有字幕但未匹配视频 {file_item.get('name','')!r}: {sub_names}")
        return []

    logger.info(f"[MediaOrganize] 字幕匹配命中: video={file_item.get('name', '')!r} mode={match_mode} count={len(matched_ops)}")
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

    logger.info(f"[MediaOrganize] 去重/失败分支字幕联动: video={file_item.get('name', '')!r} mode={match_mode} count={len(matched_ops)}")
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
        if not subs or not matched_ops:
            continue

        logger.info(f"[MediaOrganize] {log_prefix}: video={file_item.get('name', '')!r} mode={match_mode} count={len(matched_ops)}")
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
                _record_organized_source_path(subtitle_id, target_path)
                if subtitle_id.isdigit():
                    succeeded_ids.add(int(subtitle_id))
                video_id = str(op.get("video_id") or "")
                if video_id:
                    result_map.setdefault(video_id, []).append({"name": op["new_name"], "pickcode": op.get("pickcode", "")})
            await _apply_subtitle_move_results(parent_id, subs, succeeded_ids, subtitles_by_parent)
            continue

        if batch_result.get("blocked"):
            raise OneOneFiveWafBlockedError("字幕批量处理", _get_115_cooldown_remaining_seconds(), batch_result.get("error", ""))
        logger.warning(f"[MediaOrganize] 字幕批量处理失败，回退逐条处理: parent_id={parent_id}, err={batch_result.get('error', '')}")
        for op in unique_ops:
            ok = await _rename_115_file(client, op, op["new_name"], target_cid=target_cid, target_path=target_path)
            if not ok:
                logger.warning(f"[MediaOrganize] 字幕移动失败: {op['old_name']!r}")
                continue
            subtitle_id = str(op.get("id") or op.get("fid") or "")
            _record_organized_source_path(subtitle_id, target_path)
            succeeded_ids = {int(subtitle_id)} if subtitle_id.isdigit() else set()
            await _apply_subtitle_move_results(parent_id, subtitles_by_parent.get(parent_id, subs), succeeded_ids, subtitles_by_parent)
            video_id = str(op.get("video_id") or "")
            if video_id:
                result_map.setdefault(video_id, []).append({"name": op["new_name"], "pickcode": op.get("pickcode", "")})

    return result_map


async def _move_top_dir_to_failed(client, file_item: dict, source_cid_str: str,
                                  failed_dir_cid: str, moved_dirs: set,
                                  subtitles_by_parent: dict | None = None,
                                  target_path: str = ""):
    """将失败文件及同目录匹配字幕移到失败目录"""
    try:
        file_id = str(file_item.get("id") or file_item.get("fid", ""))
        if file_id and file_id not in moved_dirs:
            await _move_115_items(client, int(file_id), failed_dir_cid)
            moved_dirs.add(file_id)
            if target_path:
                _record_organized_source_path(file_id, target_path)
            logger.debug(f"[MediaOrganize] 移动失败文件: {file_item.get('name', '')} -> 整理失败目录")
        if subtitles_by_parent:
            await _move_matched_subtitles_to_target(
                client,
                file_item,
                subtitles_by_parent,
                target_cid=str(failed_dir_cid),
                target_path=target_path,
            )
    except OneOneFiveWafBlockedError:
        raise
    except Exception as e:
        logger.warning(
            f"[MediaOrganize] 移动失败文件失败: file={file_item.get('name', '')}, "
            f"source_cid={source_cid_str}, target={target_path or failed_dir_cid}, err={e}"
        )


async def _move_failed_files_batch(client, group_failed: list, source_cid: str,
                                   failed_dir_cid: str, moved_dirs: set,
                                   subtitles_by_parent: dict | None = None):
    """批量将失败文件移到失败目录，一次 API 请求"""
    ids_to_move = []
    for fi in group_failed:
        file_id = str(fi.get("id") or fi.get("fid", ""))
        if file_id and file_id not in moved_dirs:
            ids_to_move.append((file_id, fi.get("name", "")))

    if not ids_to_move:
        return

    try:
        await _move_115_items(client, [int(fid) for fid, _ in ids_to_move], failed_dir_cid)
        for file_id, name in ids_to_move:
            moved_dirs.add(file_id)
            logger.debug(f"[MediaOrganize] 移动失败文件: {name} -> 整理失败目录")
    except OneOneFiveWafBlockedError:
        logger.warning(f"[MediaOrganize] 批量移动失败文件触发115风控，停止逐个移动: count={len(ids_to_move)}")
        raise
    except Exception as e:
        logger.warning(f"[MediaOrganize] 批量移动失败文件失败: count={len(ids_to_move)}, err={e}")
        # 降级逐个移动
        for fi in group_failed:
            await _move_top_dir_to_failed(client, fi, source_cid, failed_dir_cid, moved_dirs,
                                          subtitles_by_parent=subtitles_by_parent)
        return

    if subtitles_by_parent:
        for fi in group_failed:
            file_id = str(fi.get("id") or fi.get("fid", ""))
            if file_id in moved_dirs:
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

def _iter_115_media_entries(client, cid: str) -> Iterator[dict]:
    """用 traverse_tree_with_path 递归列出源目录树，只产出视频/字幕条目，不写媒体库缓存。"""
    from p115client.tool.iterdir import traverse_tree_with_path

    global _LAST_TREE_SCAN_FINISHED_AT
    with _TREE_SCAN_LOCK:
        elapsed_since_last_scan = time.monotonic() - _LAST_TREE_SCAN_FINISHED_AT
        if elapsed_since_last_scan < _TREE_SCAN_MIN_INTERVAL_SECONDS:
            time.sleep(_TREE_SCAN_MIN_INTERVAL_SECONDS - elapsed_since_last_scan)
        scan_started_at = time.monotonic()
        try:
            with _read_lock:
                items = list(traverse_tree_with_path(
                    client,
                    cid=int(cid),
                    with_ancestors=True,
                    app="android",
                    max_workers=0,
                ))
        except Exception as e:
            if _is_115_waf_blocked(e):
                _trip_115_cooldown("扫描目录树", e)
            raise
        _LAST_TREE_SCAN_FINISHED_AT = time.monotonic()
        logger.debug(f"[MediaOrganize] 目录树遍历完成: cid={cid} 耗时={_LAST_TREE_SCAN_FINISHED_AT - scan_started_at:.2f}s")

    for item in items:
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
                },
            }


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
    except OneOneFiveWafBlockedError:
        raise
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
    except OneOneFiveWafBlockedError:
        raise
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
