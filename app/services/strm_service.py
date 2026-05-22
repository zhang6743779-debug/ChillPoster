import os
import json
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from itertools import batched
from queue import Queue, Empty
from threading import Thread, Event, Lock
from time import perf_counter
from typing import Dict, List, Optional, Tuple

from p115client import P115Client
from p115client.tool.iterdir import iter_files_with_path, traverse_tree_with_path
from p115client.tool.download import download_file, get_pic_url
from p115client.tool.attr import get_attr
from p115rsacipher import encrypt, decrypt
from core.logger import logger
from core.media_library_cache import build_task_key, save_task_snapshot
from app.services.media_organize_scrape import (
    list_missing_tmdb_metadata_for_strm,
    scrape_tmdb_metadata_for_strm_local_file,
    set_bulk_mode,
)
from app.services.media_organize_tmdb import (
    _parse_filename,
    _fetch_tmdb_data_sync,
    _load_config_data,
    _build_scraping_config,
)


# ==========================================
# 常量
# ==========================================
DEFAULT_VIDEO_EXTS = '.mp4,.mpg,.mkv,.mpeg,.ts,.vob,.iso,.m4v,.avi,.3gp,.wmv,.webm,.flv,.mov,.m2ts,.rmvb,.rm,.asf,.f4v,.m2t,.mts,.mpe,.tp,.trp,.divx,.ogv,.dv'
DEFAULT_AUDIO_EXTS = '.mp3,.flac,.wav,.m4a,.ape,.dsd,.dff,.dsf,.ac3,.dts'
DEFAULT_IMAGE_EXTS = '.jpg,.jpeg,.png,.webp,.bmp,.tiff,.tif,.ico,.gif,.svg,.heic,.avif,.raw'
DEFAULT_DATA_EXTS = '.nfo,.lrc,.srt,.pdf,.ass,.ssa,.md,.sub,.sup,.idx,.txt,.xml,.json,.smi,.vtt,.ttml,.dfxp,.scc,.bup,.ifo'
SUBTITLE_EXTS = {'.lrc', '.srt', '.ass', '.ssa', '.sub', '.sup', '.idx', '.smi', '.vtt', '.ttml', '.dfxp', '.scc'}


# 写入队列 / IO 线程数（参考 p115strmhelper）
WRITE_QUEUE_MAX = 4096
IO_WORKER_COUNT = 16
# 处理并发数（参考 p115strmhelper）
PROCESS_WORKERS = 8
# 批次大小（参考 p115strmhelper full_sync_batch_num）
BATCH_SIZE = 2000
CACHE_FLUSH_BATCHES = 5
CACHE_FLUSH_SECONDS = 10
TMDB_FALLBACK_CACHE_TTL_SECONDS = 15
TMDB_FALLBACK_WORKERS = 16
TMDB_FALLBACK_SCRAPE_WORKERS = 40
TMDB_FALLBACK_BATCH_SIZE = 2000
AUX_DOWNLOAD_WORKERS = 15
CDN_AUX_RETRY_DELAYS = (0.5, 1.5)

# 处理结果
ProcessResult = namedtuple("ProcessResult", ["status", "path", "message", "data"])

_tmdb_fallback_cache_lock = Lock()
_tmdb_fallback_cache: dict[tuple, tuple[float, dict | None]] = {}


def _build_folder_counter() -> dict:
    return {
        "strm": set(),
        "aux": set(),
    }


def _snapshot_folder_counter(counter: dict) -> dict:
    return {
        "generated_dirs": len(counter.get("strm", set())),
        "downloaded_dirs": len(counter.get("aux", set())),
    }


def _remote_to_local_dir(remote_root: str, local_root: str, remote_dir_path: str) -> str:
    rr = str(remote_root or "").rstrip("/")
    lr = str(local_root or "")
    rp = str(remote_dir_path or "").rstrip("/")
    suffix = rp[len(rr):].lstrip("/") if rr and rp.startswith(rr) else ""
    return os.path.normpath(os.path.join(lr, suffix.replace("/", os.sep))) if suffix else lr


# ==========================================
# 工具函数
# ==========================================
def _parse_exts(exts_str: str) -> set:
    if not exts_str:
        return set()
    parts = [e.strip().lower() for e in exts_str.split(',') if e.strip()]
    if len(parts) == 1 and parts[0].count('.') > 1:
        segments = parts[0].split('.')
        return {'.' + seg.strip() for seg in segments if seg.strip()}
    return set(parts)


def classify_file(filename: str, video_exts: set, audio_exts: set, image_exts: set, data_exts: set) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in video_exts:
        return 'video'
    if ext in audio_exts:
        return 'audio'
    if ext in image_exts:
        return 'image'
    if ext in SUBTITLE_EXTS:
        return 'subtitle'
    if ext in data_exts:
        return 'data'
    return 'other'


def get_strm_filename(filename: str) -> str:
    name, ext = os.path.splitext(filename)
    if ext.lower() == '.iso':
        return filename + '.strm'
    return name + '.strm'


def build_strm_url(url_base: str, pickcode: str, filename: str) -> str:
    _, ext = os.path.splitext(str(filename or ""))
    normalized_ext = ext if ext else ".mkv"
    return f"{url_base.rstrip('/')}/d/{pickcode}{normalized_ext}"


def _update_progress(run_id, name, percent, status="running", detail=None):
    from app.dependencies import update_task_progress
    update_task_progress(run_id, name, percent, status, detail=detail)


def _extract_cache_item(item: dict, parent_id: int = 0) -> Optional[Tuple[str, dict]]:
    try:
        item_id = int(item.get("id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not item_id:
        return None
    try:
        size = int(item.get("size", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    try:
        raw_parent_id = int(item.get("parent_id", item.get("cid", 0)) or 0)
    except (TypeError, ValueError):
        raw_parent_id = 0
    resolved_parent_id = raw_parent_id or int(parent_id or 0)
    data = {
        "name": str(item.get("name", "") or ""),
        "path": str(item.get("path", "") or ""),
        "pickcode": str(item.get("pickcode", item.get("pick_code", "")) or ""),
        "size": size,
        "id": item_id,
        "sha1": str(item.get("sha1", "") or ""),
        "is_dir": bool(item.get("is_dir", False)),
        "parent_id": resolved_parent_id,
    }
    return str(item_id), data


def _resolve_relative_parts(item_path: str, remote_path: str) -> tuple[str, str]:
    if item_path and remote_path:
        rp = remote_path.rstrip("/")
        if item_path.startswith(rp):
            relative = item_path[len(rp):].lstrip("/")
        else:
            relative = ""
    else:
        relative = ""
    rel_dir = os.path.dirname(relative) if relative else ""
    return relative, rel_dir


def _get_parent_remote_path(item_path: str, remote_root: str) -> str:
    normalized_path = str(item_path or "").rstrip("/")
    normalized_root = str(remote_root or "").rstrip("/")
    if not normalized_path:
        return ""
    if normalized_root and normalized_path == normalized_root:
        return ""
    return os.path.dirname(normalized_path).rstrip("/")


def _load_tmdb_scraping_config_sync():
    config_data = {}
    cfg_path = "config/media_organize.json"
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
        except Exception as e:
            logger.warning(f"[STRM] 读取媒体整理配置失败: {e}")
    return _build_scraping_config(config_data)


def _build_tmdb_plan(item: dict, remote_path: str, local_path: str, strm_path: str) -> dict:
    return {
        "filename": str(item.get("name", "") or ""),
        "remote_file_path": str(item.get("path", "") or ""),
        "local_file_path": str(strm_path or ""),
        "remote_root": str(remote_path or "").rstrip("/"),
        "local_root": str(local_path or ""),
    }


def _is_strm_season_dir_name(name: str) -> bool:
    return bool(str(name or "").lower().startswith("season "))


def _build_tmdb_scrape_group_key(local_file_path: str, media_type: str) -> str:
    local_file = os.path.normpath(str(local_file_path or ""))
    if not local_file:
        return ""
    parent = os.path.dirname(local_file)
    if media_type == "movie":
        return parent
    season_name = os.path.basename(parent)
    if _is_strm_season_dir_name(season_name):
        return os.path.dirname(parent)
    return parent


def _prune_tmdb_fallback_cache(now_ts: float):
    expired_keys = [
        key for key, (ts, _) in _tmdb_fallback_cache.items()
        if now_ts - ts > TMDB_FALLBACK_CACHE_TTL_SECONDS
    ]
    for key in expired_keys:
        _tmdb_fallback_cache.pop(key, None)


def _get_cached_tmdb_fallback_value(cache_key: tuple):
    now_ts = time.time()
    with _tmdb_fallback_cache_lock:
        _prune_tmdb_fallback_cache(now_ts)
        cached = _tmdb_fallback_cache.get(cache_key)
        if not cached:
            return None
        ts, value = cached
        if now_ts - ts > TMDB_FALLBACK_CACHE_TTL_SECONDS:
            _tmdb_fallback_cache.pop(cache_key, None)
            return None
        return value


def _set_cached_tmdb_fallback_value(cache_key: tuple, value: dict | None):
    now_ts = time.time()
    with _tmdb_fallback_cache_lock:
        _prune_tmdb_fallback_cache(now_ts)
        _tmdb_fallback_cache[cache_key] = (now_ts, value)


def _resolve_tmdb_data_with_short_cache(parsed: dict, api_key: str, task_cache: dict | None = None) -> tuple[dict | None, str]:
    tmdb_id = int(parsed.get("tmdb_id_direct") or 0)
    if not tmdb_id:
        return None, "缺少直写 TMDb ID"

    media_type = str(parsed.get("media_type", "") or "")
    season_number = parsed.get("season")
    cache_key = (tmdb_id, media_type)
    if task_cache is not None and cache_key in task_cache:
        return task_cache[cache_key], "cache_hit"

    cached = _get_cached_tmdb_fallback_value(cache_key)
    if cached is not None:
        if task_cache is not None:
            task_cache[cache_key] = cached
        return cached, "cache_hit"

    tmdb_data = _fetch_tmdb_data_sync(
        tmdb_id,
        media_type,
        api_key,
        season_number,
        parsed,
    )
    if task_cache is not None:
        task_cache[cache_key] = tmdb_data or None
    _set_cached_tmdb_fallback_value(cache_key, tmdb_data or None)
    return tmdb_data, "fetched"


def _run_tmdb_metadata_fallback(plan: dict, scraping_config, api_key: str, task_cache: dict | None = None) -> tuple[str, int, str]:
    started_at = perf_counter()
    if not api_key:
        return "skip", 0, "未配置TMDb API Key"

    filename = str(plan.get("filename", "") or "")
    remote_file_path = str(plan.get("remote_file_path", "") or "")
    local_file_path = str(plan.get("local_file_path", "") or "")
    logger.debug(f"[STRM] TMDb补齐检查开始: {remote_file_path or filename}")
    if not filename or not remote_file_path or not local_file_path:
        return "skip", 0, "缺少 TMDb 补齐所需路径"

    parsed = _parse_filename(filename, file_path=remote_file_path)
    if not parsed:
        return "skip", 0, "文件名解析失败"
    if not int(parsed.get("tmdb_id_direct") or 0):
        return "skip", 0, "缺少直写 TMDb ID"

    check_started = perf_counter()
    missing = list_missing_tmdb_metadata_for_strm(
        local_file_path,
        parsed["media_type"],
        scraping_config,
        parsed.get("season"),
        parsed.get("episode"),
    )
    logger.debug(
        f"[STRM] TMDb缺失检查完成: {remote_file_path} | 缺失:{','.join(missing) if missing else '无'} | "
        f"耗时:{perf_counter() - check_started:.2f}s"
    )
    if not missing:
        return "skip", 0, "本地元数据已完整"

    resolve_started = perf_counter()
    tmdb_data, tmdb_source = _resolve_tmdb_data_with_short_cache(parsed, api_key, task_cache)
    logger.debug(
        f"[STRM] TMDb详情获取完成: {remote_file_path} | 来源:{tmdb_source} | "
        f"耗时:{perf_counter() - resolve_started:.2f}s"
    )
    if not tmdb_data:
        return "fail", 0, "TMDb 详情获取失败"

    scrape_started = perf_counter()
    generated = scrape_tmdb_metadata_for_strm_local_file(
        local_file_path,
        tmdb_data,
        parsed["media_type"],
        scraping_config,
        season_number=parsed.get("season"),
        episode_number=parsed.get("episode"),
        overwrite=False,
    )
    scrape_elapsed = perf_counter() - scrape_started
    total_elapsed = perf_counter() - started_at
    logger.debug(
        f"[STRM] TMDb本地刮削完成: {remote_file_path} | 生成:{len(generated)} | "
        f"刮削耗时:{scrape_elapsed:.2f}s | 总耗时:{total_elapsed:.2f}s"
    )
    source_label = "短时缓存" if tmdb_source == "cache_hit" else "实时拉取"
    return "ok", len(generated), f"{','.join(missing)} | {source_label}"


def _process_tmdb_batch(
    batch_plans: list,
    scraping_config,
    api_key: str,
    stats: dict,
    log_prefix: str,
    cancel_event: Optional[Event],
) -> None:
    """处理单批 TMDb 补齐：预处理 → 并发拉取 → 分组落盘，处理完后释放 data_by_key。"""
    prepared_items: list[dict] = []
    fetch_payloads: dict[tuple, dict] = {}
    data_by_key: dict[tuple, dict | None] = {}

    for plan in batch_plans:
        if cancel_event and cancel_event.is_set():
            return
        filename = str(plan.get("filename", "") or "")
        remote_file_path = str(plan.get("remote_file_path", "") or "")
        local_file_path = str(plan.get("local_file_path", "") or "")
        if not filename or not remote_file_path or not local_file_path:
            stats["tmdb_skipped"] += 1
            continue

        parsed = _parse_filename(filename, file_path=remote_file_path)
        if not parsed:
            stats["tmdb_skipped"] += 1
            continue

        tmdb_id = int(parsed.get("tmdb_id_direct") or 0)
        if not tmdb_id:
            stats["tmdb_skipped"] += 1
            logger.debug(f"[STRM] {log_prefix} TMDb 元数据补齐跳过: 缺少直写 TMDb ID | 文件:{remote_file_path}")
            continue

        check_started = perf_counter()
        missing = list_missing_tmdb_metadata_for_strm(
            local_file_path,
            parsed["media_type"],
            scraping_config,
            parsed.get("season"),
            parsed.get("episode"),
        )
        if logger.isEnabledFor(10):  # DEBUG
            logger.debug(
                f"[STRM] TMDb缺失检查: {remote_file_path} | 缺失:{','.join(missing) if missing else '无'} | "
                f"耗时:{perf_counter() - check_started:.2f}s"
            )
        if not missing:
            stats["tmdb_skipped"] += 1
            continue

        media_type = str(parsed.get("media_type", "") or "")
        fetch_key = (tmdb_id, media_type)
        cached = _get_cached_tmdb_fallback_value(fetch_key)
        if cached is not None:
            data_by_key[fetch_key] = cached
        else:
            fetch_payloads.setdefault(fetch_key, {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "season_number": parsed.get("season"),
                "parsed": parsed,
            })

        prepared_items.append({
            "plan": plan,
            "parsed": parsed,
            "missing": missing,
            "fetch_key": fetch_key,
        })

    if not prepared_items:
        return

    if cancel_event and cancel_event.is_set():
        return

    keys_to_fetch = [key for key in fetch_payloads if key not in data_by_key]
    if keys_to_fetch:
        workers = min(TMDB_FALLBACK_WORKERS, len(keys_to_fetch))
        logger.info(f"[STRM] {log_prefix} TMDb 批次拉取: {len(keys_to_fetch)} 个唯一条目，并发 {workers}")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _fetch_tmdb_data_sync,
                    payload["tmdb_id"],
                    payload["media_type"],
                    api_key,
                    payload["season_number"],
                    payload["parsed"],
                ): key
                for key, payload in fetch_payloads.items()
                if key not in data_by_key
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    tmdb_data = future.result()
                except Exception as e:
                    tmdb_data = None
                    logger.warning(f"[STRM] {log_prefix} TMDb 条目获取异常: key={key} | {e}")
                data_by_key[key] = tmdb_data or None
                _set_cached_tmdb_fallback_value(key, tmdb_data or None)

    grouped_scrape_items: dict[str, list[dict]] = {}
    for item in prepared_items:
        local_file_path = str(item["plan"].get("local_file_path", "") or "")
        group_key = _build_tmdb_scrape_group_key(local_file_path, item["parsed"]["media_type"])
        grouped_scrape_items.setdefault(group_key, []).append(item)

    def _scrape_group(group_key: str, group_items: list[dict]) -> tuple[int, int]:
        generated_total = 0
        failed_total = 0
        for item in group_items:
            if cancel_event and cancel_event.is_set():
                break
            plan = item["plan"]
            parsed = item["parsed"]
            fetch_key = item["fetch_key"]
            remote_file_path = str(plan.get("remote_file_path", "") or "")
            local_file_path = str(plan.get("local_file_path", "") or "")
            tmdb_data = data_by_key.get(fetch_key)
            if not tmdb_data:
                failed_total += 1
                logger.warning(f"[STRM] {log_prefix} TMDb 元数据补齐失败: TMDb 详情获取失败 | 文件:{remote_file_path}")
                continue
            scrape_started = perf_counter()
            try:
                generated = scrape_tmdb_metadata_for_strm_local_file(
                    local_file_path,
                    tmdb_data,
                    parsed["media_type"],
                    scraping_config,
                    season_number=parsed.get("season"),
                    episode_number=parsed.get("episode"),
                    overwrite=False,
                )
                generated_total += len(generated)
                if logger.isEnabledFor(10):
                    logger.debug(
                        f"[STRM] TMDb本地刮削完成: {remote_file_path} | 生成:{len(generated)} | "
                        f"耗时:{perf_counter() - scrape_started:.2f}s"
                    )
            except Exception as e:
                failed_total += 1
                logger.warning(f"[STRM] {log_prefix} TMDb 本地刮削异常: {remote_file_path}: {e}")
        return generated_total, failed_total

    scrape_workers = min(TMDB_FALLBACK_SCRAPE_WORKERS, len(grouped_scrape_items))
    if scrape_workers <= 1:
        for group_key, group_items in grouped_scrape_items.items():
            generated_count, failed_count = _scrape_group(group_key, group_items)
            stats["tmdb_generated"] += generated_count
            stats["tmdb_failed"] += failed_count
    else:
        with ThreadPoolExecutor(max_workers=scrape_workers) as executor:
            futures = {}
            for group_key, group_items in grouped_scrape_items.items():
                if cancel_event and cancel_event.is_set():
                    break
                futures[executor.submit(_scrape_group, group_key, group_items)] = group_key
            for future in as_completed(futures):
                try:
                    generated_count, failed_count = future.result()
                except Exception as e:
                    generated_count, failed_count = 0, len(grouped_scrape_items[futures[future]])
                    logger.warning(f"[STRM] {log_prefix} TMDb 目录组落盘异常: {futures[future]}: {e}")
                stats["tmdb_generated"] += generated_count
                stats["tmdb_failed"] += failed_count

    # 显式释放本批次 TMDb 数据
    data_by_key.clear()
    prepared_items.clear()
    grouped_scrape_items.clear()


_BULK_MODE_THRESHOLD = 1000


def _apply_tmdb_fallbacks(plans: list, scraping_config, api_key: str, stats: dict, log_prefix: str, cancel_event: Optional[Event] = None):
    if not plans:
        return
    if not api_key:
        stats["tmdb_skipped"] += len(plans)
        logger.info(f"[STRM] {log_prefix} TMDb 元数据补齐跳过: 未配置TMDb API Key | 项目:{len(plans)}")
        return

    started_at = perf_counter()
    total = len(plans)
    batch_size = TMDB_FALLBACK_BATCH_SIZE
    num_batches = (total + batch_size - 1) // batch_size
    bulk = total >= _BULK_MODE_THRESHOLD
    logger.info(f"[STRM] {log_prefix} TMDb 元数据补齐开始: {total} 项，分 {num_batches} 批，每批 {batch_size}{'（批量模式）' if bulk else ''}")

    if bulk:
        set_bulk_mode(True)

    try:
        for batch_idx, batch_start in enumerate(range(0, total, batch_size)):
            if cancel_event and cancel_event.is_set():
                logger.info(f"[STRM] {log_prefix} TMDb 元数据补齐取消: 第 {batch_idx + 1} 批前")
                return
            batch_plans = plans[batch_start: batch_start + batch_size]
            batch_started = perf_counter()
            _process_tmdb_batch(
                batch_plans,
                scraping_config,
                api_key,
                stats,
                log_prefix,
                cancel_event,
            )
            logger.info(
                f"[STRM] {log_prefix} 进度 {min(batch_start + batch_size, total)}/{total} | "
                f"批次 {batch_idx + 1}/{num_batches} | 生成:{stats['tmdb_generated']} 跳过:{stats['tmdb_skipped']} 失败:{stats['tmdb_failed']} | "
                f"批次耗时:{perf_counter() - batch_started:.1f}s"
            )
    finally:
        if bulk:
            set_bulk_mode(False)

    logger.info(
        f"[STRM] {log_prefix} TMDb 元数据补齐完成: {total} 项 | "
        f"生成:{stats['tmdb_generated']} 跳过:{stats['tmdb_skipped']} 失败:{stats['tmdb_failed']} | "
        f"总耗时:{perf_counter() - started_at:.1f}s"
    )


# ==========================================
# IO 写入（参考 p115strmhelper __io_writer_worker + __flush_write_buffer）
# ==========================================
def _io_writer_worker(write_queue: Queue, result_queue: Queue, cancel_event: Optional[Event] = None):
    """从 write_queue 取任务批量写入 STRM 文件，结果放入 result_queue"""
    while True:
        tasks: List[Tuple[str, str, str]] = []
        try:
            first = write_queue.get()
            if first is None:
                result_queue.put(None)
                write_queue.task_done()
                break
            tasks.append(first)

            while len(tasks) < 64:
                try:
                    extra = write_queue.get_nowait()
                    if extra is None:
                        if not (cancel_event and cancel_event.is_set()):
                            _flush_buffer(tasks, result_queue)
                        for _ in tasks:
                            write_queue.task_done()
                        result_queue.put(None)
                        write_queue.task_done()
                        return
                    tasks.append(extra)
                except Empty:
                    break

            if cancel_event and cancel_event.is_set():
                for _ in tasks:
                    result_queue.put(ProcessResult("cancelled", None, None, None))
                continue

            _flush_buffer(tasks, result_queue)
        finally:
            for _ in tasks:
                try:
                    write_queue.task_done()
                except ValueError:
                    pass


def _flush_buffer(tasks: List[Tuple[str, str, str]], result_queue: Queue):
    """批量写入 STRM 文件"""
    for strm_path, strm_url, filename in tasks:
        try:
            os.makedirs(os.path.dirname(strm_path), exist_ok=True)
            with open(strm_path, "w", encoding="utf-8") as f:
                f.write(strm_url)
            result_queue.put(ProcessResult("success", strm_path, None, None))
        except Exception as e:
            logger.error(f"[STRM] 写入失败 {strm_path}: {e}")
            result_queue.put(ProcessResult("fail", strm_path, str(e), None))


# ==========================================
# 附属文件下载（standard 或 cdn）
# ==========================================
_UA = "Mozilla/5.0 (Linux; Android 13; 23013RK75C Build/TKQ1.221114.001) AppleWebKit/537.36 Chrome/123.0.0.0 Mobile Safari/537.36"


def _ensure_parent_dir(local_path: str):
    parent = os.path.dirname(local_path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _get_file_sha1(client: P115Client, file_id: int, pickcode: str, sha1: str = "") -> str:
    if sha1:
        return str(sha1)
    if file_id:
        attr = get_attr(client, file_id)
        got = (attr or {}).get("sha1")
        if got:
            return str(got)
    raise ValueError("无法获取文件 sha1")


def _fetch_download_url_cdn(pickcode: str, cookie: str) -> str:
    api_url = "https://proapi.115.com/android/2.0/ufile/download"
    payload = f'{{"pick_code":"{pickcode}"}}'
    encrypted_data = encrypt(payload.encode("utf-8")).decode("utf-8")
    headers = {
        "User-Agent": _UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookie,
    }

    import httpx
    with httpx.Client(timeout=20, follow_redirects=True, verify=False) as hc:
        resp = hc.post(api_url, data={"data": encrypted_data}, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    if not data.get("state"):
        raise ValueError(f"CDN接口返回失败: {data}")

    decrypted = decrypt(data["data"])
    data_obj = json.loads(decrypted)
    url = data_obj.get("url", {}).get("url") if isinstance(data_obj.get("url"), dict) else data_obj.get("url")
    if not url:
        raise ValueError(f"CDN接口未返回直链: {data_obj}")
    return str(url)


def _describe_download_error(err: Exception) -> str:
    try:
        import httpx
        if isinstance(err, httpx.HTTPStatusError):
            resp = err.response
            req = err.request
            reason = getattr(resp, "reason_phrase", "") or ""
            method = getattr(req, "method", "") or ""
            url = str(getattr(req, "url", "") or "")
            return f"HTTP {resp.status_code} {reason} {method} {url}".strip()
    except Exception:
        pass
    return str(err)


def _is_retryable_download_error(err: Exception) -> bool:
    try:
        import httpx
        if isinstance(err, httpx.HTTPStatusError):
            return err.response.status_code in {403, 408, 429, 500, 502, 503, 504}
        if isinstance(err, (httpx.TimeoutException, httpx.TransportError)):
            return True
    except Exception:
        pass
    return False


def _download_by_url(url: str, local_path: str, cancel_event: Optional[Event] = None):
    import httpx
    if cancel_event and cancel_event.is_set():
        raise CancelledError()
    _ensure_parent_dir(local_path)
    tmp_path = f"{local_path}.{time.time_ns()}.tmp"
    headers = {"User-Agent": _UA}
    try:
        with httpx.Client(headers=headers, timeout=120, follow_redirects=True, verify=False) as hc:
            with hc.stream("GET", url) as r:
                r.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        if cancel_event and cancel_event.is_set():
                            raise CancelledError()
                        f.write(chunk)
        if cancel_event and cancel_event.is_set():
            raise CancelledError()
        os.replace(tmp_path, local_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _download_aux_file(
    client: P115Client,
    pickcode: str,
    local_path: str,
    filename: str,
    file_class: str,
    file_id: int = 0,
    sha1: str = "",
    download_mode: str = "cdn",
    cookie: str = "",
    cancel_event: Optional[Event] = None,
) -> Tuple[str, str, str]:
    def _check_cancel() -> None:
        if cancel_event and cancel_event.is_set():
            raise CancelledError()

    def _download_standard() -> None:
        _check_cancel()
        if file_class == "image":
            try:
                sha1_value = _get_file_sha1(client, file_id, pickcode, sha1)
                _check_cancel()
                url = get_pic_url(client, sha1_value, user_agent=_UA, app="android")
                if not url:
                    raise ValueError("图片链接为空")
                _check_cancel()
                download_file(client, pickcode, local_path, resume=True, user_agent=_UA, app="android")
                logger.info(f"[STRM] 图片附属下载完成: {filename} | pic_url:{url}")
            except CancelledError:
                raise
            except Exception as pic_err:
                logger.warning(f"[STRM] 图片接口下载失败，回退普通下载: {filename}: {pic_err}")
                _check_cancel()
                download_file(client, pickcode, local_path, resume=True, user_agent=_UA, app="android")
                logger.info(f"[STRM] 图片附属回退下载完成: {filename}")
        else:
            _check_cancel()
            download_file(client, pickcode, local_path, resume=True, user_agent=_UA, app="android")
            logger.info(f"[STRM] 非图片附属下载完成: {filename}")

    try:
        _check_cancel()
        _ensure_parent_dir(local_path)
        logger.info(f"[STRM] 开始下载附属文件: {filename} | 类型:{file_class} | 模式:{download_mode} | 路径:{local_path}")

        if download_mode == "cdn":
            if not cookie:
                logger.warning(f"[STRM] CDN下载跳过，缺少 cookie，回退standard: {filename}")
                _download_standard()
                return ("ok", filename, "")

            total_attempts = len(CDN_AUX_RETRY_DELAYS) + 1
            last_cdn_err: Exception | None = None
            for attempt in range(1, total_attempts + 1):
                try:
                    _check_cancel()
                    url = _fetch_download_url_cdn(pickcode, cookie)
                    _download_by_url(url, local_path, cancel_event=cancel_event)
                    logger.info(f"[STRM] CDN附属下载完成: {filename}")
                    return ("ok", filename, "")
                except CancelledError:
                    return ("cancelled", filename, "cancelled")
                except Exception as cdn_err:
                    last_cdn_err = cdn_err
                    err_msg = _describe_download_error(cdn_err)
                    can_retry = attempt < total_attempts and _is_retryable_download_error(cdn_err)
                    if not can_retry:
                        logger.warning(f"[STRM] CDN下载失败，回退standard: {filename}: {err_msg}")
                        break
                    delay = CDN_AUX_RETRY_DELAYS[attempt - 1]
                    logger.warning(
                        f"[STRM] CDN下载失败，{delay:.1f}s 后重试({attempt}/{total_attempts}): {filename}: {err_msg}"
                    )
                    time.sleep(delay)

            try:
                _download_standard()
                return ("ok", filename, "")
            except Exception as standard_err:
                cdn_msg = _describe_download_error(last_cdn_err) if last_cdn_err else ""
                standard_msg = _describe_download_error(standard_err)
                raise RuntimeError(f"CDN失败: {cdn_msg}; standard失败: {standard_msg}") from standard_err

        _download_standard()
        return ("ok", filename, "")
    except CancelledError:
        return ("cancelled", filename, "cancelled")
    except Exception as e:
        return ("fail", filename, str(e))


def _batch_download_aux(
    client: P115Client,
    items: list,
    local_path: str,
    overwrite_mode: str,
    download_mode: str = "standard",
    cookie: str = "",
    cancel_event: Optional[Event] = None,
) -> Tuple[int, int]:
    """批量下载附属文件并输出下载条目日志"""
    if not items:
        return (0, 0)

    to_download = []
    for it in items:
        target_dir = os.path.join(local_path, it["rel_dir"]) if it["rel_dir"] else local_path
        local_file = os.path.join(target_dir, it["filename"])
        it["_local_path"] = local_file

        if overwrite_mode == "skip" and os.path.exists(local_file):
            try:
                if os.path.getsize(local_file) == it["size"]:
                    logger.info(f"[STRM] 跳过附属文件(已存在且大小一致): {it['filename']}")
                    continue
            except OSError:
                pass
        to_download.append(it)

    if not to_download:
        return (0, 0)

    downloaded = 0
    failed = 0

    def _dl_one(it):
        return _download_aux_file(
            client=client,
            pickcode=it["pickcode"],
            local_path=it["_local_path"],
            filename=it["filename"],
            file_class=it.get("_file_class", "data"),
            file_id=it.get("_id", 0),
            sha1=it.get("_sha1", ""),
            download_mode=download_mode,
            cookie=cookie,
            cancel_event=cancel_event,
        )

    with ThreadPoolExecutor(max_workers=AUX_DOWNLOAD_WORKERS) as executor:
        futures = {executor.submit(_dl_one, it): it for it in to_download}
        for future in as_completed(futures):
            if cancel_event and cancel_event.is_set():
                break
            status, fname, msg = future.result()
            if status == "ok":
                downloaded += 1
                logger.info(f"[STRM] 下载附属文件成功: {fname}")
            elif status == "cancelled":
                logger.info(f"[STRM] 下载附属文件已取消: {fname}")
            else:
                failed += 1
                logger.warning(f"[STRM] 下载附属文件失败: {fname}: {msg}")

    return (downloaded, failed)


# ==========================================
# 单条文件处理（使用 iter_files_with_path 标准化字段）
# ==========================================
def _process_single_item(
    item: dict,
    remote_path: str,
    local_path: str,
    url_base: str,
    video_exts: set,
    audio_exts: set,
    image_exts: set,
    data_exts: set,
    sync_video: bool,
    download_aux: bool,
    download_tmdb_metadata: bool,
    min_video_size_mb: int,
    overwrite_mode: str,
    dl_executor: Optional[ThreadPoolExecutor],
    client: P115Client,
    write_queue: Queue,
    dl_futures: list,
    aux_download_mode: str = "cdn",
    cookie: str = "",
    cancel_event: Optional[Event] = None,
) -> Optional[ProcessResult]:
    try:
        if cancel_event and cancel_event.is_set():
            return ProcessResult("cancelled", None, "cancelled", None)

        if item.get("is_dir"):
            return None

        filename = item.get("name", "")
        pickcode = item.get("pickcode", "")
        if not filename or not pickcode:
            return None

        item_path = item.get("path", "")
        _, rel_dir = _resolve_relative_parts(item_path, remote_path)
        target_dir = os.path.join(local_path, rel_dir) if rel_dir else local_path

        file_class = classify_file(filename, video_exts, audio_exts, image_exts, data_exts)
        force_strm_overwrite = bool(item.get("_force_strm_overwrite"))

        if file_class in ("video", "audio") and sync_video:
            strm_name = get_strm_filename(filename)
            strm_path = os.path.join(target_dir, strm_name)

            if overwrite_mode == "skip" and not force_strm_overwrite and os.path.exists(strm_path):
                return ProcessResult("skip", filename, "STRM已存在", None)

            if file_class == "video" and min_video_size_mb > 0:
                try:
                    size_mb = int(item.get("size", 0)) / (1024 * 1024)
                except (ValueError, TypeError):
                    size_mb = 0
                if size_mb < min_video_size_mb:
                    return ProcessResult("skip", filename, "视频小于最小体积限制", None)

            strm_url = build_strm_url(url_base, pickcode, filename)
            write_queue.put((strm_path, strm_url, filename))
            data = {
                "strm_path": strm_path,
                "tmdb_plan": _build_tmdb_plan(item, remote_path, local_path, strm_path) if download_tmdb_metadata else None,
            }
            return ProcessResult("submitted", None, None, data)

        elif file_class == "subtitle" or (file_class in ("image", "data") and download_aux):
            local_file = os.path.join(target_dir, filename)
            if file_class == "subtitle":
                skip_reason = "字幕文件已存在"
            else:
                skip_reason = "附属文件已存在"
            # 跳过已存在且大小一致的文件
            if os.path.exists(local_file) and overwrite_mode == "skip":
                try:
                    local_size = os.path.getsize(local_file)
                    remote_size = int(item.get("size", 0))
                    if local_size == remote_size:
                        return ProcessResult("skip", filename, skip_reason, None)
                except (ValueError, TypeError):
                    pass
            if dl_executor:
                file_id = int(item.get("id", 0) or 0)
                file_size = int(item.get("size", 0) or 0)
                file_sha1 = str(item.get("sha1", "") or "")
                fut = dl_executor.submit(
                    _download_aux_file,
                    client,
                    pickcode,
                    local_file,
                    filename,
                    file_class,
                    file_id,
                    file_sha1,
                    aux_download_mode,
                    cookie,
                    cancel_event,
                )
                dl_futures.append((fut, {
                    "pickcode": pickcode,
                    "filename": filename,
                    "rel_dir": rel_dir,
                    "size": file_size,
                    "_id": file_id,
                    "_file_class": file_class,
                    "_sha1": file_sha1,
                    "_local_path": local_file,
                }))
            return ProcessResult("download_submitted", filename, None, None)

        return ProcessResult("skip", filename, "不在同步范围", None)

    except Exception as e:
        logger.error(f"[STRM] 处理出错 {item}: {e}")
        return ProcessResult("fail", str(item), str(e), None)


# ==========================================
# StrmService 核心类
# ==========================================
class StrmService:
    CONFIG_PATH = "config/strm_config.json"

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=2)

    # ---- 配置读写 ----
    def load_config(self) -> dict:
        if not os.path.exists(self.CONFIG_PATH):
            return {"sync_tasks": []}
        try:
            with open(self.CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"sync_tasks": []}
            tasks = data.get("sync_tasks")
            if not isinstance(tasks, list):
                data["sync_tasks"] = []
                return data
            try:
                from app.routers.config_302 import get_config_302_sync
                from app.routers.strm import hydrate_strm_task
                cfg302 = get_config_302_sync()
                data["sync_tasks"] = [hydrate_strm_task(task, cfg302) for task in tasks if isinstance(task, dict)]
            except Exception as e:
                logger.warning(f"[STRM] 注入自动播放前缀失败: {e}")
                data["sync_tasks"] = [task for task in tasks if isinstance(task, dict)]
            return data
        except Exception as e:
            logger.error(f"[STRM] 读取配置失败: {e}")
            return {"sync_tasks": []}

    def save_config(self, config: dict):
        os.makedirs(os.path.dirname(self.CONFIG_PATH), exist_ok=True)
        with open(self.CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)

    # ---- Cookie / Client ----
    def _get_cookie(self, drive_index: int) -> str:
        cfg_path = "config/config_302.json"
        if not os.path.exists(cfg_path):
            raise Exception("302 配置不存在，请先配置 115 Cookie")
        with open(cfg_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        drives = cfg.get("drives", [])
        drive_cfg = drives[0] if isinstance(drives, list) and drives else cfg.get("drive", {})
        if not isinstance(drive_cfg, dict) or not drive_cfg:
            raise Exception("未配置 115 账号")
        cookie = str(drive_cfg.get("cookie", "") or "").strip()
        if not cookie:
            raise Exception("115 Cookie 未配置")
        return cookie

    def _get_client(self, drive_index: int) -> P115Client:
        return P115Client(self._get_cookie(drive_index))

    def _build_remote_strm_paths(
        self,
        client: P115Client,
        remote_path: str,
        local_path: str,
        video_exts: set,
        audio_exts: set,
        image_exts: set,
        data_exts: set,
    ) -> set:
        """遍历远端目录，构建应存在的本地 strm 路径集合"""
        remote_strm_paths: set = set()
        rp = remote_path.rstrip("/")
        iter_kwargs = {
            "cid": client.fs_dir_getid_app(remote_path)["id"],
            "with_ancestors": True,
            "cooldown": 1.5,
            "app": "ios",
        }
        for item in iter_files_with_path(client, **iter_kwargs):
            if item.get("is_dir"):
                continue
            filename = item.get("name", "")
            item_path = item.get("path", "")
            if not filename or not item_path:
                continue
            file_class = classify_file(filename, video_exts, audio_exts, image_exts, data_exts)
            if file_class not in ("video", "audio"):
                continue
            relative = item_path[len(rp):].lstrip("/") if item_path.startswith(rp) else ""
            rel_dir = os.path.dirname(relative) if relative else ""
            target_dir = os.path.join(local_path, rel_dir) if rel_dir else local_path
            remote_strm_paths.add(os.path.normpath(os.path.join(target_dir, get_strm_filename(filename))))
        return remote_strm_paths

    @staticmethod
    def _remove_same_stem_metadata(local_dir: str, strm_filename: str) -> int:
        deleted = 0
        try:
            entries = os.listdir(local_dir)
        except OSError as e:
            logger.error(f"[STRM] 读取目录失败 {local_dir}: {e}")
            return 0

        stem = os.path.splitext(strm_filename)[0]
        for sibling in entries:
            if sibling == strm_filename or sibling.lower().endswith('.strm'):
                continue
            if not sibling.startswith(stem):
                continue
            sib_path = os.path.normpath(os.path.join(local_dir, sibling))
            if not os.path.isfile(sib_path):
                continue
            try:
                os.remove(sib_path)
                deleted += 1
                logger.debug(f"[STRM] 删除关联元数据: {sib_path}")
            except Exception as e:
                logger.error(f"[STRM] 删除失败 {sib_path}: {e}")
        return deleted

    @staticmethod
    def _rename_same_stem_metadata(local_dir: str, old_strm_filename: str, new_strm_filename: str) -> int:
        renamed = 0
        try:
            entries = os.listdir(local_dir)
        except OSError as e:
            logger.error(f"[STRM] 读取目录失败 {local_dir}: {e}")
            return 0

        old_stem = os.path.splitext(old_strm_filename)[0]
        new_stem = os.path.splitext(new_strm_filename)[0]
        for sibling in entries:
            if sibling == old_strm_filename or sibling.lower().endswith('.strm'):
                continue
            if not sibling.startswith(old_stem):
                continue
            old_path = os.path.normpath(os.path.join(local_dir, sibling))
            if not os.path.isfile(old_path):
                continue
            new_name = new_stem + sibling[len(old_stem):]
            new_path = os.path.normpath(os.path.join(local_dir, new_name))
            try:
                os.replace(old_path, new_path)
                renamed += 1
                logger.debug(f"[STRM] 重命名关联元数据: {old_path} -> {new_path}")
            except Exception as e:
                logger.error(f"[STRM] 重命名失败 {old_path} -> {new_path}: {e}")
        return renamed

    @staticmethod
    def _cleanup_local_orphans(local_path: str, remote_strm_paths: set, keep_dirs: Optional[set[str]] = None) -> int:
        """清理本地孤儿 strm、同名元数据和无 strm 目录"""
        deleted = 0
        keep_dirs = {os.path.normpath(p) for p in (keep_dirs or set()) if str(p or "").strip()}

        for root, dirs, files in os.walk(local_path):
            for f in files:
                if not f.lower().endswith('.strm'):
                    continue
                full = os.path.normpath(os.path.join(root, f))
                if full in remote_strm_paths:
                    continue
                try:
                    os.remove(full)
                    deleted += 1
                    logger.debug(f"[STRM] 删除孤儿strm: {full}")
                except Exception as e:
                    logger.error(f"[STRM] 删除失败 {full}: {e}")
                    continue

                deleted += StrmService._remove_same_stem_metadata(root, f)

        for root, dirs, files in os.walk(local_path, topdown=False):
            if root == local_path:
                continue
            normalized_root = os.path.normpath(root)
            if normalized_root in keep_dirs:
                continue
            has_strm = any(
                fname.lower().endswith('.strm')
                for _, _, fnames in os.walk(root)
                for fname in fnames
            )
            if not has_strm:
                import shutil
                try:
                    shutil.rmtree(root)
                    deleted += 1
                    logger.debug(f"[STRM] 删除无strm目录: {root}")
                except Exception as e:
                    logger.error(f"[STRM] 删除目录失败 {root}: {e}")

        return deleted

    def cleanup_orphan_for_task(self, task_config: dict, reason: str = "") -> dict:
        """按单个 STRM 任务配置执行孤儿清理"""
        task_name = task_config.get("name", "未知任务")
        drive_index = task_config.get("drive_index", 0)
        remote_path = str(task_config.get("remote_path", "") or "").rstrip("/")
        local_path = task_config.get("local_path", "")

        if not remote_path or not local_path:
            return {"status": "skip", "task": task_name, "deleted": 0, "message": "缺少 remote/local 路径"}

        video_exts = _parse_exts(task_config.get("video_exts_str", DEFAULT_VIDEO_EXTS))
        audio_exts = _parse_exts(task_config.get("audio_exts_str", DEFAULT_AUDIO_EXTS))
        image_exts = _parse_exts(DEFAULT_IMAGE_EXTS)
        data_exts = _parse_exts(DEFAULT_DATA_EXTS)

        try:
            client = self._get_client(drive_index)
            remote_strm_paths = self._build_remote_strm_paths(
                client,
                remote_path,
                local_path,
                video_exts,
                audio_exts,
                image_exts,
                data_exts,
            )
            deleted = self._cleanup_local_orphans(local_path, remote_strm_paths)
            logger.info(f"[STRM] 孤儿清理完成: {task_name} | 远端媒体:{len(remote_strm_paths)} 删除:{deleted} 原因:{reason or 'manual'}")
            return {"status": "ok", "task": task_name, "deleted": deleted, "remote_media": len(remote_strm_paths)}
        except Exception as e:
            logger.error(f"[STRM] 孤儿清理失败: {task_name}: {e}")
            return {"status": "error", "task": task_name, "deleted": 0, "message": str(e)}

    def cleanup_orphan_for_remote_path(self, remote_path: str, reason: str = "") -> dict:
        """按 remote_path 匹配配置任务并执行孤儿清理"""
        rp = str(remote_path or "").rstrip("/")
        config = self.load_config()
        tasks = config.get("sync_tasks", [])
        matched = [t for t in tasks if str(t.get("remote_path", "") or "").rstrip("/") == rp]

        if not matched:
            return {"status": "skip", "matched": 0, "deleted": 0, "message": "未匹配到 STRM 任务"}

        results = [self.cleanup_orphan_for_task(task, reason=reason) for task in matched]
        deleted = sum(int(r.get("deleted", 0)) for r in results)
        has_error = any(r.get("status") == "error" for r in results)
        return {
            "status": "error" if has_error else "ok",
            "matched": len(matched),
            "deleted": deleted,
            "results": results,
        }

    def cleanup_orphan_for_remote_subpath(self, remote_path: str, reason: str = "") -> dict:
        """按 remote 子路径匹配任务并仅清理对应本地子树下的孤儿 STRM"""
        rp = str(remote_path or "").rstrip("/")
        if not rp:
            return {"status": "skip", "matched": 0, "deleted": 0, "message": "缺少 remote_path"}

        config = self.load_config()
        tasks = config.get("sync_tasks", [])
        matched = []
        for task in tasks:
            task_remote = str(task.get("remote_path", "") or "").rstrip("/")
            if not task_remote:
                continue
            if rp == task_remote or rp.startswith(task_remote + "/"):
                matched.append(task)

        if not matched:
            return {"status": "skip", "matched": 0, "deleted": 0, "message": "未匹配到 STRM 任务"}

        results = []
        total_deleted = 0
        has_error = False
        for task in matched:
            task_name = task.get("name", "未知任务")
            drive_index = task.get("drive_index", 0)
            task_remote = str(task.get("remote_path", "") or "").rstrip("/")
            local_root = str(task.get("local_path", "") or "")
            if not task_remote or not local_root:
                results.append({"status": "skip", "task": task_name, "deleted": 0, "message": "缺少 remote/local 路径"})
                continue

            suffix = rp[len(task_remote):].lstrip("/") if rp.startswith(task_remote) else ""
            local_subpath = os.path.normpath(os.path.join(local_root, suffix.replace("/", os.sep))) if suffix else local_root

            video_exts = _parse_exts(task.get("video_exts_str", DEFAULT_VIDEO_EXTS))
            audio_exts = _parse_exts(task.get("audio_exts_str", DEFAULT_AUDIO_EXTS))
            image_exts = _parse_exts(DEFAULT_IMAGE_EXTS)
            data_exts = _parse_exts(DEFAULT_DATA_EXTS)

            try:
                client = self._get_client(drive_index)
                remote_strm_paths = self._build_remote_strm_paths(
                    client,
                    rp,
                    local_subpath,
                    video_exts,
                    audio_exts,
                    image_exts,
                    data_exts,
                )
                deleted = self._cleanup_local_orphans(local_subpath, remote_strm_paths) if os.path.exists(local_subpath) else 0
                results.append({
                    "status": "ok",
                    "task": task_name,
                    "deleted": deleted,
                    "remote_media": len(remote_strm_paths),
                    "local_subpath": local_subpath,
                })
                total_deleted += deleted
                logger.info(f"[STRM] 子路径孤儿清理完成: {task_name} | 远端:{rp} 本地:{local_subpath} 远端媒体:{len(remote_strm_paths)} 删除:{deleted} 原因:{reason or 'manual'}")
            except Exception as e:
                has_error = True
                logger.error(f"[STRM] 子路径孤儿清理失败: {task_name}: {e}")
                results.append({"status": "error", "task": task_name, "deleted": 0, "message": str(e), "local_subpath": local_subpath})

        return {
            "status": "error" if has_error else "ok",
            "matched": len(matched),
            "deleted": total_deleted,
            "results": results,
        }

    def remove_local_folder_for_remote_subpath(self, remote_path: str, reason: str = "") -> dict:
        """按 remote 子路径匹配任务并直接删除对应本地目录"""
        rp = str(remote_path or "").rstrip("/")
        if not rp:
            return {"status": "skip", "matched": 0, "deleted": 0, "message": "缺少 remote_path"}

        config = self.load_config()
        tasks = config.get("sync_tasks", [])
        matched = []
        for task in tasks:
            task_remote = str(task.get("remote_path", "") or "").rstrip("/")
            if not task_remote:
                continue
            if rp == task_remote or rp.startswith(task_remote + "/"):
                matched.append(task)

        if not matched:
            return {"status": "skip", "matched": 0, "deleted": 0, "message": "未匹配到 STRM 任务"}

        import shutil
        results = []
        total_deleted = 0
        has_error = False
        for task in matched:
            task_name = task.get("name", "未知任务")
            task_remote = str(task.get("remote_path", "") or "").rstrip("/")
            local_root = str(task.get("local_path", "") or "")
            if not task_remote or not local_root:
                results.append({"status": "skip", "task": task_name, "deleted": 0, "message": "缺少 remote/local 路径"})
                continue

            suffix = rp[len(task_remote):].lstrip("/") if rp.startswith(task_remote) else ""
            local_subpath = os.path.normpath(os.path.join(local_root, suffix.replace("/", os.sep))) if suffix else local_root

            try:
                if os.path.exists(local_subpath):
                    shutil.rmtree(local_subpath)
                    deleted = 1
                    logger.info(f"[STRM] 已直接删除本地目录: {task_name} | 远端:{rp} 本地:{local_subpath} 原因:{reason or 'manual'}")
                else:
                    deleted = 0
                results.append({
                    "status": "ok",
                    "task": task_name,
                    "deleted": deleted,
                    "local_subpath": local_subpath,
                })
                total_deleted += deleted
            except Exception as e:
                has_error = True
                logger.error(f"[STRM] 直接删除本地目录失败: {task_name}: {e}")
                results.append({"status": "error", "task": task_name, "deleted": 0, "message": str(e), "local_subpath": local_subpath})

        return {
            "status": "error" if has_error else "ok",
            "matched": len(matched),
            "deleted": total_deleted,
            "results": results,
        }

    def remove_local_strm_for_remote_file(self, remote_file_path: str, reason: str = "") -> dict:
        """按 remote 文件路径匹配任务并直接删除对应本地 strm 与同 stem 元数据"""
        rp = str(remote_file_path or "").rstrip("/")
        if not rp:
            return {"status": "skip", "matched": 0, "deleted": 0, "message": "缺少 remote_file_path"}

        remote_dir = os.path.dirname(rp)
        remote_name = os.path.basename(rp)
        if not remote_dir or not remote_name:
            return {"status": "skip", "matched": 0, "deleted": 0, "message": "remote_file_path 无效"}

        config = self.load_config()
        tasks = config.get("sync_tasks", [])
        matched = []
        for task in tasks:
            task_remote = str(task.get("remote_path", "") or "").rstrip("/")
            if not task_remote:
                continue
            if remote_dir == task_remote or remote_dir.startswith(task_remote + "/"):
                matched.append(task)

        if not matched:
            return {"status": "skip", "matched": 0, "deleted": 0, "message": "未匹配到 STRM 任务"}

        results = []
        total_deleted = 0
        has_error = False
        for task in matched:
            task_name = task.get("name", "未知任务")
            task_remote = str(task.get("remote_path", "") or "").rstrip("/")
            local_root = str(task.get("local_path", "") or "")
            if not task_remote or not local_root:
                results.append({"status": "skip", "task": task_name, "deleted": 0, "message": "缺少 remote/local 路径"})
                continue

            suffix = remote_dir[len(task_remote):].lstrip("/") if remote_dir.startswith(task_remote) else ""
            local_dir = os.path.normpath(os.path.join(local_root, suffix.replace("/", os.sep))) if suffix else local_root
            strm_filename = get_strm_filename(remote_name)
            local_strm_path = os.path.normpath(os.path.join(local_dir, strm_filename))

            try:
                deleted = 0
                if os.path.exists(local_strm_path):
                    os.remove(local_strm_path)
                    deleted += 1
                    logger.info(f"[STRM] 已直接删除本地STRM: {task_name} | 远端:{rp} 本地:{local_strm_path} 原因:{reason or 'manual'}")
                    deleted += self._remove_same_stem_metadata(local_dir, strm_filename)
                results.append({
                    "status": "ok",
                    "task": task_name,
                    "deleted": deleted,
                    "local_strm_path": local_strm_path,
                })
                total_deleted += deleted
            except Exception as e:
                has_error = True
                logger.error(f"[STRM] 直接删除本地STRM失败: {task_name}: {e}")
                results.append({"status": "error", "task": task_name, "deleted": 0, "message": str(e), "local_strm_path": local_strm_path})

        return {
            "status": "error" if has_error else "ok",
            "matched": len(matched),
            "deleted": total_deleted,
            "results": results,
        }

    def rename_local_folder_for_remote_subpath(self, old_remote_path: str, new_remote_path: str, reason: str = "") -> dict:
        """按 remote 子路径匹配任务并直接重命名对应本地目录"""
        old_rp = str(old_remote_path or "").rstrip("/")
        new_rp = str(new_remote_path or "").rstrip("/")
        if not old_rp or not new_rp:
            return {"status": "skip", "matched": 0, "renamed": 0, "message": "缺少 old/new remote_path"}

        config = self.load_config()
        tasks = config.get("sync_tasks", [])
        matched = []
        for task in tasks:
            task_remote = str(task.get("remote_path", "") or "").rstrip("/")
            if not task_remote:
                continue
            if (old_rp == task_remote or old_rp.startswith(task_remote + "/")) and (
                new_rp == task_remote or new_rp.startswith(task_remote + "/")
            ):
                matched.append(task)

        if not matched:
            return {"status": "skip", "matched": 0, "renamed": 0, "message": "未匹配到 STRM 任务"}

        results = []
        total_renamed = 0
        has_error = False
        for task in matched:
            task_name = task.get("name", "未知任务")
            task_remote = str(task.get("remote_path", "") or "").rstrip("/")
            local_root = str(task.get("local_path", "") or "")
            if not task_remote or not local_root:
                results.append({"status": "skip", "task": task_name, "renamed": 0, "message": "缺少 remote/local 路径"})
                continue

            old_suffix = old_rp[len(task_remote):].lstrip("/") if old_rp.startswith(task_remote) else ""
            new_suffix = new_rp[len(task_remote):].lstrip("/") if new_rp.startswith(task_remote) else ""
            old_local_path = os.path.normpath(os.path.join(local_root, old_suffix.replace("/", os.sep))) if old_suffix else local_root
            new_local_path = os.path.normpath(os.path.join(local_root, new_suffix.replace("/", os.sep))) if new_suffix else local_root

            try:
                renamed = 0
                if os.path.exists(old_local_path) and old_local_path != new_local_path:
                    os.makedirs(os.path.dirname(new_local_path), exist_ok=True)
                    os.replace(old_local_path, new_local_path)
                    renamed = 1
                    logger.info(f"[STRM] 已直接重命名本地目录: {task_name} | {old_local_path} -> {new_local_path} 原因:{reason or 'manual'}")
                results.append({
                    "status": "ok",
                    "task": task_name,
                    "renamed": renamed,
                    "old_local_path": old_local_path,
                    "new_local_path": new_local_path,
                })
                total_renamed += renamed
            except Exception as e:
                has_error = True
                logger.error(f"[STRM] 重命名本地目录失败: {task_name}: {e}")
                results.append({"status": "error", "task": task_name, "renamed": 0, "message": str(e), "old_local_path": old_local_path, "new_local_path": new_local_path})

        return {
            "status": "error" if has_error else "ok",
            "matched": len(matched),
            "renamed": total_renamed,
            "results": results,
        }

    def rename_local_strm_for_remote_file(self, old_remote_file_path: str, new_remote_file_path: str, reason: str = "") -> dict:
        """按 remote 文件路径匹配任务并直接重命名对应本地 strm 与同 stem 元数据"""
        old_rp = str(old_remote_file_path or "").rstrip("/")
        new_rp = str(new_remote_file_path or "").rstrip("/")
        if not old_rp or not new_rp:
            return {"status": "skip", "matched": 0, "renamed": 0, "message": "缺少 old/new remote_file_path"}

        old_remote_dir = os.path.dirname(old_rp)
        old_remote_name = os.path.basename(old_rp)
        new_remote_dir = os.path.dirname(new_rp)
        new_remote_name = os.path.basename(new_rp)
        if not old_remote_dir or not old_remote_name or not new_remote_dir or not new_remote_name:
            return {"status": "skip", "matched": 0, "renamed": 0, "message": "remote_file_path 无效"}

        config = self.load_config()
        tasks = config.get("sync_tasks", [])
        matched = []
        for task in tasks:
            task_remote = str(task.get("remote_path", "") or "").rstrip("/")
            if not task_remote:
                continue
            if (old_remote_dir == task_remote or old_remote_dir.startswith(task_remote + "/")) and (
                new_remote_dir == task_remote or new_remote_dir.startswith(task_remote + "/")
            ):
                matched.append(task)

        if not matched:
            return {"status": "skip", "matched": 0, "renamed": 0, "message": "未匹配到 STRM 任务"}

        results = []
        total_renamed = 0
        has_error = False
        for task in matched:
            task_name = task.get("name", "未知任务")
            task_remote = str(task.get("remote_path", "") or "").rstrip("/")
            local_root = str(task.get("local_path", "") or "")
            if not task_remote or not local_root:
                results.append({"status": "skip", "task": task_name, "renamed": 0, "message": "缺少 remote/local 路径"})
                continue

            old_suffix = old_remote_dir[len(task_remote):].lstrip("/") if old_remote_dir.startswith(task_remote) else ""
            new_suffix = new_remote_dir[len(task_remote):].lstrip("/") if new_remote_dir.startswith(task_remote) else ""
            old_local_dir = os.path.normpath(os.path.join(local_root, old_suffix.replace("/", os.sep))) if old_suffix else local_root
            new_local_dir = os.path.normpath(os.path.join(local_root, new_suffix.replace("/", os.sep))) if new_suffix else local_root
            old_strm_filename = get_strm_filename(old_remote_name)
            new_strm_filename = get_strm_filename(new_remote_name)
            old_local_strm_path = os.path.normpath(os.path.join(old_local_dir, old_strm_filename))
            new_local_strm_path = os.path.normpath(os.path.join(new_local_dir, new_strm_filename))

            try:
                renamed = 0
                if os.path.exists(old_local_strm_path):
                    os.makedirs(new_local_dir, exist_ok=True)
                    os.replace(old_local_strm_path, new_local_strm_path)
                    renamed += 1
                    logger.info(f"[STRM] 已直接重命名本地STRM: {task_name} | {old_local_strm_path} -> {new_local_strm_path} 原因:{reason or 'manual'}")
                    renamed += self._rename_same_stem_metadata(old_local_dir, old_strm_filename, new_strm_filename)
                results.append({
                    "status": "ok",
                    "task": task_name,
                    "renamed": renamed,
                    "old_local_strm_path": old_local_strm_path,
                    "new_local_strm_path": new_local_strm_path,
                })
                total_renamed += renamed
            except Exception as e:
                has_error = True
                logger.error(f"[STRM] 重命名本地STRM失败: {task_name}: {e}")
                results.append({"status": "error", "task": task_name, "renamed": 0, "message": str(e), "old_local_strm_path": old_local_strm_path, "new_local_strm_path": new_local_strm_path})

        return {
            "status": "error" if has_error else "ok",
            "matched": len(matched),
            "renamed": total_renamed,
            "results": results,
        }

    def ensure_local_dir_for_remote_path(
        self,
        remote_dir_path: str,
        *,
        reason: str = "",
    ) -> dict:
        rp = str(remote_dir_path or "").rstrip("/")
        if not rp:
            return {"status": "skip", "matched": 0, "created": 0, "message": "缺少 remote_dir_path"}

        config = self.load_config()
        tasks = config.get("sync_tasks", [])
        matched = self._match_task_for_path(rp, tasks)
        if not matched:
            return {"status": "skip", "matched": 0, "created": 0, "message": "未匹配到 STRM 任务"}

        _, task = matched
        task_name = task.get("name", "未知任务")
        task_remote = str(task.get("remote_path", "") or "").rstrip("/")
        local_root = str(task.get("local_path", "") or "")
        if not task_remote or not local_root:
            return {"status": "skip", "matched": 1, "created": 0, "task": task_name, "message": "缺少 remote/local 路径"}

        suffix = rp[len(task_remote):].lstrip("/") if rp.startswith(task_remote) else ""
        local_dir = os.path.normpath(os.path.join(local_root, suffix.replace("/", os.sep))) if suffix else local_root

        try:
            os.makedirs(local_dir, exist_ok=True)
            logger.info(f"[STRM] 已创建本地目录: {task_name} | 远端:{rp} 本地:{local_dir} 原因:{reason or 'event'}")
            return {"status": "ok", "matched": 1, "created": 1, "task": task_name, "local_path": local_dir}
        except Exception as e:
            logger.error(f"[STRM] 创建本地目录失败: {task_name} | 远端:{rp}: {e}")
            return {"status": "error", "matched": 1, "created": 0, "task": task_name, "local_path": local_dir, "message": str(e)}

    def download_aux_for_remote_file(
        self,
        remote_file_path: str,
        *,
        pickcode: str = "",
        file_class: str = "data",
        file_id: int = 0,
        sha1: str = "",
        file_size: int = 0,
        reason: str = "",
    ) -> dict:
        rp = str(remote_file_path or "").rstrip("/")
        if not rp:
            return {"status": "skip", "matched": 0, "downloaded": 0, "message": "缺少 remote_file_path"}

        remote_dir = os.path.dirname(rp)
        filename = os.path.basename(rp)
        if not remote_dir or not filename:
            return {"status": "skip", "matched": 0, "downloaded": 0, "message": "remote_file_path 无效"}

        config = self.load_config()
        tasks = config.get("sync_tasks", [])
        matched = self._match_task_for_path(rp, tasks) or self._match_task_for_path(remote_dir, tasks)
        if not matched:
            return {"status": "skip", "matched": 0, "downloaded": 0, "message": "未匹配到 STRM 任务"}

        _, task = matched
        task_name = task.get("name", "未知任务")
        task_remote = str(task.get("remote_path", "") or "").rstrip("/")
        local_root = str(task.get("local_path", "") or "")
        if not task_remote or not local_root:
            return {"status": "skip", "matched": 1, "downloaded": 0, "task": task_name, "message": "缺少 remote/local 路径"}

        suffix = remote_dir[len(task_remote):].lstrip("/") if remote_dir.startswith(task_remote) else ""
        local_dir = os.path.normpath(os.path.join(local_root, suffix.replace("/", os.sep))) if suffix else local_root
        local_path = os.path.normpath(os.path.join(local_dir, filename))

        overwrite_mode = str(task.get("overwrite", "skip") or "skip").lower()
        if overwrite_mode == "skip" and os.path.exists(local_path):
            try:
                if int(file_size or 0) > 0 and os.path.getsize(local_path) == int(file_size or 0):
                    return {
                        "status": "skip",
                        "matched": 1,
                        "downloaded": 0,
                        "task": task_name,
                        "local_path": local_path,
                        "message": "文件已存在且大小一致",
                    }
            except OSError:
                pass

        drive_index = int(task.get("drive_index", 0) or 0)
        aux_download_mode = "cdn"

        try:
            client = self._get_client(drive_index)
            if file_id and (not pickcode or not sha1 or not int(file_size or 0)):
                try:
                    attr = get_attr(client, int(file_id))
                except Exception:
                    attr = {}
                if not pickcode:
                    pickcode = str((attr or {}).get("pickcode") or (attr or {}).get("pick_code") or (attr or {}).get("pc") or "")
                if not sha1:
                    sha1 = str((attr or {}).get("sha1") or "")
                if not int(file_size or 0):
                    try:
                        file_size = int((attr or {}).get("size", 0) or 0)
                    except (TypeError, ValueError):
                        file_size = 0

            if not pickcode:
                return {"status": "skip", "matched": 1, "downloaded": 0, "task": task_name, "local_path": local_path, "message": "缺少 pickcode"}

            cookie = self._get_cookie(drive_index) if aux_download_mode == "cdn" else ""
            status, _, message = _download_aux_file(
                client=client,
                pickcode=pickcode,
                local_path=local_path,
                filename=filename,
                file_class=file_class,
                file_id=int(file_id or 0),
                sha1=sha1,
                download_mode=aux_download_mode,
                cookie=cookie,
            )
            if status == "ok":
                logger.info(f"[STRM] 远端附属同步完成: {task_name} | 远端:{rp} 本地:{local_path} 原因:{reason or 'event'}")
                return {"status": "ok", "matched": 1, "downloaded": 1, "task": task_name, "local_path": local_path}
            if status == "cancelled":
                return {"status": "cancelled", "matched": 1, "downloaded": 0, "task": task_name, "local_path": local_path, "message": message}
            return {"status": "error", "matched": 1, "downloaded": 0, "task": task_name, "local_path": local_path, "message": message}
        except Exception as e:
            logger.error(f"[STRM] 远端附属同步失败: {task_name} | 远端:{rp}: {e}")
            return {"status": "error", "matched": 1, "downloaded": 0, "task": task_name, "local_path": local_path, "message": str(e)}

    def _build_incremental_stats(self) -> dict:
        return {
            "generated": 0,
            "generated_dirs": 0,
            "downloaded": 0,
            "downloaded_dirs": 0,
            "download_failed": 0,
            "tmdb_generated": 0,
            "tmdb_skipped": 0,
            "tmdb_failed": 0,
            "failed": 0,
            "skipped": 0,
            "skip_reasons": {},
            "retry_success": 0,
            "retry_failed": 0,
            "matched_items": 0,
        }

    @staticmethod
    def _match_task_for_path(remote_path: str, tasks: list) -> Optional[Tuple[int, dict]]:
        path = str(remote_path or "").rstrip("/")
        if not path:
            return None
        matched: Optional[Tuple[int, dict]] = None
        matched_len = -1
        for idx, task in enumerate(tasks):
            task_remote = str(task.get("remote_path", "") or "").rstrip("/")
            if not task_remote:
                continue
            if path == task_remote or path.startswith(task_remote + "/"):
                if len(task_remote) > matched_len:
                    matched = (idx, task)
                    matched_len = len(task_remote)
        return matched

    def _process_incremental_task_items(
        self,
        task_config: dict,
        items: list,
        client: P115Client,
        cookie: str = "",
        cancel_event: Optional[Event] = None,
    ) -> dict:
        task_name = task_config.get("name", "增量同步")
        remote_path = str(task_config.get("remote_path", "") or "").rstrip("/")
        local_path = task_config.get("local_path", "")
        url_base = str(task_config.get("strm_url_base", "") or "")
        if not url_base:
            raise Exception("无法生成 STRM 播放地址：未找到可用局域网 IPv4 或代理端口")
        sync_video = True
        download_aux = task_config.get("download_auxiliary", True)
        download_tmdb_metadata = task_config.get("download_tmdb_metadata", False)
        min_video_size_mb = task_config.get("min_video_size_mb", 0)
        overwrite_mode = task_config.get("overwrite", "skip")
        aux_download_mode = "cdn"

        video_exts = _parse_exts(task_config.get("video_exts_str", DEFAULT_VIDEO_EXTS))
        audio_exts = _parse_exts(task_config.get("audio_exts_str", DEFAULT_AUDIO_EXTS))
        image_exts = _parse_exts(DEFAULT_IMAGE_EXTS)
        data_exts = _parse_exts(task_config.get("data_exts_str", DEFAULT_DATA_EXTS))

        stats = self._build_incremental_stats()
        folder_counter = _build_folder_counter()
        completed_dl_futures: set = set()
        cancel_event = cancel_event or Event()
        write_queue: Queue = Queue(maxsize=WRITE_QUEUE_MAX)
        result_queue: Queue = Queue()
        io_threads: List[Thread] = []
        dl_futures = []
        dl_retry_items = []

        sample_path = str((items[0].get("path", "") if items else "") or remote_path).rstrip("/")
        logger.info(f"[STRM] 增量任务开始: {task_name} | 条目:{len(items)} | 任务路径:{sample_path}")

        scraping_config = _load_tmdb_scraping_config_sync() if download_tmdb_metadata else None
        tmdb_api_key = ""
        if download_tmdb_metadata:
            try:
                from core.configs import global_config
                tmdb_api_key = str(global_config.tmdb_key or "")
            except Exception:
                tmdb_api_key = ""

        def _record_skip(reason: str):
            key = str(reason or "unknown")
            stats["skip_reasons"][key] = int(stats["skip_reasons"].get(key, 0) or 0) + 1

        def _collect_finished_downloads(block: bool = False):
            for future, meta in dl_futures:
                if future in completed_dl_futures:
                    continue
                if not block and not future.done():
                    continue
                try:
                    res = future.result()
                    if res and res[0] == "ok":
                        stats["downloaded"] += 1
                        local_file_path = meta.get("_local_path", "")
                        if local_file_path:
                            folder_counter["aux"].add(os.path.dirname(local_file_path))
                            stats.update(_snapshot_folder_counter(folder_counter))
                    elif res and res[0] == "fail":
                        stats["download_failed"] += 1
                        stats["failed"] += 1
                        dl_retry_items.append(meta)
                    elif res and res[0] == "cancelled":
                        pass
                except Exception as e:
                    stats["download_failed"] += 1
                    stats["failed"] += 1
                    logger.warning(f"[STRM] 增量附属下载异常: {meta.get('filename', '')}: {e}")
                    dl_retry_items.append(meta)
                finally:
                    completed_dl_futures.add(future)

        for _ in range(IO_WORKER_COUNT):
            t = Thread(target=_io_writer_worker, args=(write_queue, result_queue, cancel_event), daemon=True)
            t.start()
            io_threads.append(t)

        def result_collector():
            finished = 0
            while finished < IO_WORKER_COUNT:
                try:
                    result = result_queue.get()
                    if result is None:
                        finished += 1
                        continue
                    if result.status == "success":
                        stats["generated"] += 1
                        if result.path:
                            folder_counter["strm"].add(os.path.dirname(result.path))
                            stats.update(_snapshot_folder_counter(folder_counter))
                    elif result.status == "fail":
                        stats["failed"] += 1
                finally:
                    result_queue.task_done()

        collector_thread = Thread(target=result_collector, daemon=True)
        collector_thread.start()

        dl_executor = ThreadPoolExecutor(max_workers=15)
        tmdb_plans = []
        try:
            for item in items:
                if cancel_event and cancel_event.is_set():
                    logger.info(f"[STRM] 增量任务收到取消信号: {task_name}")
                    break
                if item.get("is_dir"):
                    continue
                stats["matched_items"] += 1
                result = _process_single_item(
                    item,
                    remote_path,
                    local_path,
                    url_base,
                    video_exts,
                    audio_exts,
                    image_exts,
                    data_exts,
                    sync_video,
                    download_aux,
                    download_tmdb_metadata,
                    min_video_size_mb,
                    overwrite_mode,
                    dl_executor,
                    client,
                    write_queue,
                    dl_futures,
                    aux_download_mode,
                    cookie,
                    cancel_event,
                )
                if result and result.status == "fail":
                    stats["failed"] += 1
                elif result and result.status == "cancelled":
                    logger.info(f"[STRM] 增量任务取消中: {task_name}")
                    break
                elif result and result.status == "skip":
                    stats["skipped"] += 1
                    _record_skip(result.message)
                elif result and result.status == "submitted" and download_tmdb_metadata:
                    tmdb_plan = (result.data or {}).get("tmdb_plan") if result.data else None
                    if tmdb_plan:
                        tmdb_plans.append(tmdb_plan)

            write_queue.join()
            for _ in range(IO_WORKER_COUNT):
                write_queue.put(None)
            for t in io_threads:
                t.join(timeout=10)
            result_queue.join()
            collector_thread.join(timeout=5)

            if cancel_event and cancel_event.is_set():
                _collect_finished_downloads(block=False)
                dl_executor.shutdown(wait=False, cancel_futures=True)
            else:
                _collect_finished_downloads(block=True)
                dl_executor.shutdown(wait=True, cancel_futures=False)

            if dl_retry_items and not (cancel_event and cancel_event.is_set()):
                logger.info(f"[STRM] 增量附属重试开始，共 {len(dl_retry_items)} 项")
                retry_ok, retry_fail = _batch_download_aux(
                    client,
                    dl_retry_items,
                    local_path,
                    "overwrite",
                    aux_download_mode,
                    cookie,
                    cancel_event,
                )
                stats["downloaded"] += retry_ok
                stats["failed"] -= retry_ok
                stats["retry_success"] += retry_ok
                stats["retry_failed"] += retry_fail

            if download_tmdb_metadata and tmdb_plans and not (cancel_event and cancel_event.is_set()):
                _apply_tmdb_fallbacks(tmdb_plans, scraping_config, tmdb_api_key, stats, "增量", cancel_event)
        finally:
            dl_executor.shutdown(wait=False, cancel_futures=True)

        was_cancelled = bool(cancel_event and cancel_event.is_set())
        logger.info(
            f"[STRM] 增量任务{'已取消' if was_cancelled else '完成'}: {task_name} | 生成:{stats['generated']} 下载:{stats['downloaded']} "
            f"TMDb补齐:{stats['tmdb_generated']} TMDb跳过:{stats['tmdb_skipped']} TMDb失败:{stats['tmdb_failed']} "
            f"跳过:{stats['skipped']} 失败:{stats['failed']} 重试成功:{stats['retry_success']} 重试失败:{stats['retry_failed']}"
        )
        return {
            "status": "cancelled" if was_cancelled else ("error" if stats["failed"] else "ok"),
            "task": task_name,
            **stats,
        }

    def process_incremental_items(self, items: list, cancel_event: Optional[Event] = None) -> dict:
        if cancel_event and cancel_event.is_set():
            return {"status": "cancelled", "matched_tasks": 0, "matched_items": 0, "results": []}
        if not items:
            return {"status": "skip", "matched_tasks": 0, "matched_items": 0, "results": []}

        config = self.load_config()
        tasks = config.get("sync_tasks", [])
        grouped: Dict[int, dict] = {}
        unmatched_items = 0

        for item in items:
            if cancel_event and cancel_event.is_set():
                return {
                    "status": "cancelled",
                    "matched_tasks": 0,
                    "matched_items": 0,
                    "unmatched_items": unmatched_items,
                    "results": [],
                }
            remote_item_path = str(item.get("path", "") or "")
            match = self._match_task_for_path(remote_item_path, tasks)
            if not match:
                unmatched_items += 1
                continue
            idx, task = match
            if idx not in grouped:
                grouped[idx] = {"task": task, "items": []}
            grouped[idx]["items"].append(item)

        if not grouped:
            return {
                "status": "skip",
                "matched_tasks": 0,
                "matched_items": 0,
                "unmatched_items": unmatched_items,
                "results": [],
            }

        results = []
        summary = self._build_incremental_stats()
        cancelled = False
        for data in grouped.values():
            if cancel_event and cancel_event.is_set():
                cancelled = True
                break
            task = data["task"]
            task_items = data["items"]
            drive_index = task.get("drive_index", 0)
            client = self._get_client(drive_index)
            cookie = self._get_cookie(drive_index)
            task_result = self._process_incremental_task_items(
                task,
                task_items,
                client,
                cookie,
                cancel_event=cancel_event,
            )
            results.append(task_result)
            for key in ("generated", "generated_dirs", "downloaded", "downloaded_dirs", "download_failed", "failed", "skipped", "retry_success", "retry_failed", "matched_items"):
                summary[key] += int(task_result.get(key, 0) or 0)
            for reason, count in (task_result.get("skip_reasons") or {}).items():
                summary["skip_reasons"][reason] = int(summary["skip_reasons"].get(reason, 0) or 0) + int(count or 0)
            if task_result.get("status") == "cancelled":
                cancelled = True
                break

        return {
            "status": (
                "cancelled"
                if cancelled or (cancel_event and cancel_event.is_set())
                else ("error" if summary["failed"] else "ok")
            ),
            "matched_tasks": len(results),
            "matched_items": summary["matched_items"],
            "unmatched_items": unmatched_items,
            "generated": summary["generated"],
            "generated_dirs": summary["generated_dirs"],
            "downloaded": summary["downloaded"],
            "downloaded_dirs": summary["downloaded_dirs"],
            "download_failed": summary["download_failed"],
            "failed": summary["failed"],
            "skipped": summary["skipped"],
            "skip_reasons": summary["skip_reasons"],
            "retry_success": summary["retry_success"],
            "retry_failed": summary["retry_failed"],
            "results": results,
        }

    # ==========================================
    # 全量同步（参考 p115strmhelper generate_strm_files 架构）
    # ==========================================
    def run_full_sync(self, task_config: dict, run_id: str):
        """全量同步：先纯扫描目录树，再写缓存，最后执行同步动作。"""
        from app.dependencies import ACTIVE_TASKS

        task_name = task_config.get("name", "全量同步")
        drive_index = task_config.get("drive_index", 0)
        remote_path = task_config.get("remote_path", "")
        local_path = task_config.get("local_path", "")
        url_base = str(task_config.get("strm_url_base", "") or "")
        if not url_base:
            raise Exception("无法生成 STRM 播放地址：未找到可用局域网 IPv4 或代理端口")
        sync_video = True
        download_aux = task_config.get("download_auxiliary", True)
        download_tmdb_metadata = task_config.get("download_tmdb_metadata", False)
        min_video_size_mb = task_config.get("min_video_size_mb", 0)
        overwrite_mode = task_config.get("overwrite", "skip")
        aux_download_mode = "cdn"
        cookie = self._get_cookie(drive_index)

        video_exts = _parse_exts(task_config.get("video_exts_str", DEFAULT_VIDEO_EXTS))
        audio_exts = _parse_exts(task_config.get("audio_exts_str", DEFAULT_AUDIO_EXTS))
        image_exts = _parse_exts(DEFAULT_IMAGE_EXTS)
        data_exts = _parse_exts(DEFAULT_DATA_EXTS)

        logger.info(f"[STRM] 全量同步开始: {task_name} | {remote_path} -> {local_path} | 覆盖模式: {overwrite_mode}")

        start_time = perf_counter()

        def _notify_task(status: str, detail: str = "", stats: Optional[dict] = None, elapsed_seconds: Optional[float] = None):
            try:
                from app.services.wechat_service import wechat_notify_service
                from app.services.telegram_service import telegram_notify_service

                payload = stats.copy() if stats else {}
                elapsed_value = elapsed_seconds
                if elapsed_value is None:
                    elapsed_value = perf_counter() - start_time
                elapsed_text = f"{elapsed_value:.1f}s" if elapsed_value is not None else ""
                notify_kwargs = {
                    "task_name": f"STRM任务: {task_name}",
                    "status": status,
                    "detail": detail,
                    "elapsed": elapsed_text,
                    "scanned": int(payload.get("scanned", 0) or 0),
                    "scanned_dirs": int(payload.get("scanned_dirs", 0) or 0),
                    "generated": int(payload.get("generated", 0) or 0),
                    "downloaded": int(payload.get("downloaded", 0) or 0),
                    "download_failed": int(payload.get("download_failed", 0) or 0),
                    "skipped": int(payload.get("skipped", 0) or 0),
                    "deleted": int(payload.get("deleted", 0) or 0),
                    "failed": int(payload.get("failed", 0) or 0),
                    "retry_success": int(payload.get("retry_success", 0) or 0),
                    "retry_failed": int(payload.get("retry_failed", 0) or 0),
                }
                wechat_notify_service.notify_task_complete(**notify_kwargs)
                telegram_notify_service.notify_task_complete(**notify_kwargs)
            except Exception as notify_err:
                logger.warning(f"[STRM] 任务通知发送失败: {notify_err}")

        try:
            client = self._get_client(drive_index)

            dir_resp = client.fs_dir_getid_app(remote_path)
            if not dir_resp or not dir_resp.get("id"):
                _update_progress(run_id, f"STRM: {task_name}", 100, "error")
                logger.error(f"[STRM] 远程路径不存在: {remote_path}")
                _notify_task("error", f"远程路径不存在: {remote_path}")
                return

            cid = dir_resp["id"]
            task_key = build_task_key(drive_index, remote_path)
            stats = {
                "scanned": 0,
                "scanned_dirs": 0,
                "scanned_files": 0,
                "generated": 0,
                "generated_dirs": 0,
                "downloaded": 0,
                "downloaded_dirs": 0,
                "download_failed": 0,
                "tmdb_generated": 0,
                "tmdb_skipped": 0,
                "tmdb_failed": 0,
                "failed": 0,
                "skipped": 0,
                "skip_reasons": {},
                "deleted": 0,
                "retry_success": 0,
                "retry_failed": 0,
            }
            current_run_items: Dict[str, dict] = {}
            collected_items: List[dict] = []
            remote_strm_paths: set = set()
            remote_aux_paths: set = set()
            remote_keep_dirs: set = set()
            dir_id_by_path: Dict[str, int] = {str(remote_path or "").rstrip("/"): int(cid)}
            cancelled = False
            cancel_event = Event()

            logger.info(f"[STRM] 阶段1开始：扫描目录树 {task_name}")
            tree_iter_kwargs = {
                "cid": cid,
                "with_ancestors": True,
                "app": "android",
                "max_workers": 0,
            }

            for batch in batched(
                traverse_tree_with_path(client, **tree_iter_kwargs),
                BATCH_SIZE,
            ):
                if ACTIVE_TASKS.get(run_id, {}).get("cancel_requested"):
                    cancelled = True
                    cancel_event.set()
                    break

                for item in batch:
                    collected_items.append(item)
                    stats["scanned"] += 1
                    item_path = str(item.get("path", "") or "").rstrip("/")
                    parent_path = _get_parent_remote_path(item_path, remote_path)
                    derived_parent_id = dir_id_by_path.get(parent_path, 0) if parent_path else 0
                    if item.get("is_dir"):
                        stats["scanned_dirs"] += 1
                        if item_path:
                            remote_keep_dirs.add(_remote_to_local_dir(remote_path, local_path, item_path))
                    else:
                        stats["scanned_files"] += 1

                    cache_item = _extract_cache_item(item, parent_id=derived_parent_id)
                    if cache_item:
                        item_key, item_data = cache_item
                        current_run_items[item_key] = item_data
                        if item_data.get("is_dir") and item_path:
                            dir_id_by_path[item_path] = int(item_data.get("id", 0) or 0)

                    if item.get("is_dir"):
                        continue

                    filename = item.get("name", "")
                    item_path = item.get("path", "")
                    if not filename or not item_path:
                        continue

                    fc = classify_file(filename, video_exts, audio_exts, image_exts, data_exts)
                    rp = remote_path.rstrip("/")
                    relative = item_path[len(rp):].lstrip("/") if item_path.startswith(rp) else ""
                    rel_dir = os.path.dirname(relative) if relative else ""
                    target_dir = os.path.join(local_path, rel_dir) if rel_dir else local_path
                    if fc in ("video", "audio"):
                        remote_strm_paths.add(os.path.normpath(os.path.join(target_dir, get_strm_filename(filename))))
                    elif fc in ("image", "data"):
                        remote_aux_paths.add(os.path.normpath(os.path.join(target_dir, filename)))

                _update_progress(
                    run_id,
                    f"STRM扫描中: {task_name}",
                    min(40, stats["scanned"] // 1000),
                    "running",
                    detail=stats.copy(),
                )

            scan_elapsed = perf_counter() - start_time
            if cancelled:
                _update_progress(run_id, f"STRM: {task_name} (已取消)", 100, "stopped", detail=stats.copy())
                logger.info(f"[STRM] 阶段1取消：{task_name} | 耗时 {scan_elapsed:.1f}s")
                _notify_task("stopped", "扫描阶段取消", stats.copy(), scan_elapsed)
                return

            logger.info(f"[STRM] 阶段1完成：扫描结束 {task_name} | 总数:{stats['scanned']} 目录:{stats['scanned_dirs']} 文件:{stats['scanned_files']}")
            _update_progress(run_id, f"STRM写缓存中: {task_name}", 45, "running", detail=stats.copy())
            cache_start = perf_counter()
            cache_item_count = len(current_run_items)
            logger.info(f"[STRM] 阶段2开始：写媒体库缓存 {task_name} | 条目:{cache_item_count} | 跳过常驻索引重建")
            save_task_snapshot(
                task_key,
                current_run_items,
                meta={
                    "last_status": "scanned",
                    "updated_at": time.time(),
                },
                rebuild_resident_index=False,
            )
            cache_elapsed = perf_counter() - cache_start
            logger.info(f"[STRM] 阶段2完成：媒体库缓存已更新 {task_name} | 条目:{cache_item_count} | 耗时 {cache_elapsed:.1f}s")

            if ACTIVE_TASKS.get(run_id, {}).get("cancel_requested"):
                cancelled = True
                cancel_event.set()
                _update_progress(run_id, f"STRM: {task_name} (已取消)", 100, "stopped", detail=stats.copy())
                logger.info(f"[STRM] 阶段2后取消：{task_name}")
                _notify_task("stopped", "写缓存后取消", stats.copy())
                return

            logger.info(f"[STRM] 阶段3开始：执行同步动作 {task_name}")

            scraping_config = _load_tmdb_scraping_config_sync() if download_tmdb_metadata else None
            tmdb_api_key = ""
            if download_tmdb_metadata:
                try:
                    from core.configs import global_config
                    tmdb_api_key = str(global_config.tmdb_key or "")
                except Exception:
                    tmdb_api_key = ""

            def _record_skip(reason: str):
                key = str(reason or "unknown")
                stats["skip_reasons"][key] = int(stats["skip_reasons"].get(key, 0) or 0) + 1

            completed_dl_futures: set = set()
            folder_counter = _build_folder_counter()

            def _collect_finished_downloads(block: bool = False):
                nonlocal cancelled
                for future, meta in dl_futures:
                    if future in completed_dl_futures:
                        continue
                    if not block and not future.done():
                        continue
                    try:
                        res = future.result()
                        if res and res[0] == "ok":
                            stats["downloaded"] += 1
                            local_path = meta.get("_local_path", "")
                            if local_path:
                                folder_counter["aux"].add(os.path.dirname(local_path))
                                stats.update(_snapshot_folder_counter(folder_counter))
                            logger.debug(f"[STRM] 下载附属文件: {res[1]}")
                        elif res and res[0] == "fail":
                            stats["download_failed"] += 1
                            stats["failed"] += 1
                            logger.warning(f"[STRM] 下载附属文件失败: {res[1]}: {res[2]}")
                            if not cancelled:
                                dl_retry_items.append(meta)
                        elif res and res[0] == "cancelled":
                            cancelled = True
                            cancel_event.set()
                    except CancelledError:
                        cancelled = True
                        cancel_event.set()
                    except Exception as e:
                        stats["download_failed"] += 1
                        stats["failed"] += 1
                        logger.warning(f"[STRM] 下载附属文件异常: {meta.get('filename', '')}: {e}")
                        if not cancelled:
                            dl_retry_items.append(meta)
                    finally:
                        completed_dl_futures.add(future)

            write_queue: Queue = Queue(maxsize=WRITE_QUEUE_MAX)
            result_queue: Queue = Queue()
            io_threads: List[Thread] = []
            for _ in range(IO_WORKER_COUNT):
                t = Thread(target=_io_writer_worker, args=(write_queue, result_queue, cancel_event), daemon=True)
                t.start()
                io_threads.append(t)

            def result_collector():
                finished = 0
                while finished < IO_WORKER_COUNT:
                    try:
                        result = result_queue.get()
                        if result is None:
                            finished += 1
                            continue
                        if result.status == "success":
                            stats["generated"] += 1
                            if result.path:
                                folder_counter["strm"].add(os.path.dirname(result.path))
                                stats.update(_snapshot_folder_counter(folder_counter))
                        elif result.status == "fail":
                            stats["failed"] += 1
                        elif result.status == "cancelled":
                            pass
                    finally:
                        result_queue.task_done()

            collector_thread = Thread(target=result_collector, daemon=True)
            collector_thread.start()

            dl_executor = ThreadPoolExecutor(max_workers=15)
            dl_futures = []
            dl_retry_items = []
            tmdb_plans = []
            processed_files = 0

            created_empty_dirs = 0
            for local_dir in sorted(remote_keep_dirs, key=lambda p: (p.count(os.sep), p)):
                if not local_dir:
                    continue
                try:
                    existed = os.path.isdir(local_dir)
                    os.makedirs(local_dir, exist_ok=True)
                    if not existed:
                        created_empty_dirs += 1
                except Exception as e:
                    stats["failed"] += 1
                    logger.error(f"[STRM] 创建本地目录失败: {local_dir}: {e}")
            if created_empty_dirs:
                logger.info(f"[STRM] 已创建本地空目录 {created_empty_dirs} 个: {task_name}")

            for batch in batched(collected_items, BATCH_SIZE):
                if ACTIVE_TASKS.get(run_id, {}).get("cancel_requested"):
                    cancelled = True
                    cancel_event.set()
                    break

                for item in batch:
                    if item.get("is_dir"):
                        continue
                    if ACTIVE_TASKS.get(run_id, {}).get("cancel_requested"):
                        cancelled = True
                        cancel_event.set()
                        break
                    try:
                        result = _process_single_item(
                            item,
                            remote_path,
                            local_path,
                            url_base,
                            video_exts,
                            audio_exts,
                            image_exts,
                            data_exts,
                            sync_video,
                            download_aux,
                            download_tmdb_metadata,
                            min_video_size_mb,
                            overwrite_mode,
                            dl_executor,
                            client,
                            write_queue,
                            dl_futures,
                            aux_download_mode,
                            cookie,
                            cancel_event,
                        )
                        processed_files += 1
                        if result and result.status == "fail":
                            stats["failed"] += 1
                        elif result and result.status == "skip":
                            stats["skipped"] += 1
                            _record_skip(result.message)
                        elif result and result.status == "submitted" and download_tmdb_metadata:
                            tmdb_plan = (result.data or {}).get("tmdb_plan") if result.data else None
                            if tmdb_plan:
                                tmdb_plans.append(tmdb_plan)
                        elif result and result.status == "cancelled":
                            cancelled = True
                            cancel_event.set()
                            break
                    except CancelledError:
                        cancelled = True
                        cancel_event.set()
                        break
                    except Exception as e:
                        stats["failed"] += 1
                        logger.error(f"[STRM] 处理出错: {e}")

                _collect_finished_downloads(block=False)
                process_total = max(stats["scanned_files"], 1)
                _update_progress(
                    run_id,
                    f"STRM同步中: {task_name}",
                    50 + min(49, processed_files * 49 // process_total),
                    "running",
                    detail=stats.copy(),
                )

                if cancelled:
                    break

            if cancelled:
                cancel_event.set()
                try:
                    while True:
                        pending = write_queue.get_nowait()
                        write_queue.task_done()
                except Empty:
                    pass
            else:
                write_queue.join()
            for _ in range(IO_WORKER_COUNT):
                write_queue.put(None)
            for t in io_threads:
                t.join(timeout=10)
            result_queue.join()
            collector_thread.join(timeout=5)

            if cancelled:
                dl_executor.shutdown(wait=False, cancel_futures=True)
            else:
                _collect_finished_downloads(block=True)
                dl_executor.shutdown(wait=True, cancel_futures=False)

            if dl_retry_items and not cancelled:
                logger.info(f"[STRM] 开始失败附属重试，共 {len(dl_retry_items)} 项")
                retry_ok, retry_fail = _batch_download_aux(
                    client,
                    dl_retry_items,
                    local_path,
                    "overwrite",
                    aux_download_mode,
                    cookie,
                    cancel_event,
                )
                stats["downloaded"] += retry_ok
                stats["failed"] -= retry_ok
                stats["retry_success"] += retry_ok
                stats["retry_failed"] += retry_fail
                logger.info(f"[STRM] 失败附属重试完成: 成功 {retry_ok} | 失败 {retry_fail}")

            if download_tmdb_metadata and tmdb_plans and not cancelled:
                _apply_tmdb_fallbacks(tmdb_plans, scraping_config, tmdb_api_key, stats, "全量", cancel_event)

            elapsed = perf_counter() - start_time
            stats["elapsed_seconds"] = elapsed
            if cancelled:
                _update_progress(run_id, f"STRM: {task_name} (已取消)", 100, "stopped", detail=stats.copy())
                logger.info(f"[STRM] 阶段3取消：{task_name} | 耗时 {elapsed:.1f}s")
                _notify_task("stopped", "同步阶段取消", stats.copy(), elapsed)
                return

            stats["deleted"] += self._cleanup_local_orphans(local_path, remote_strm_paths, keep_dirs=remote_keep_dirs)
            finish_cache_start = perf_counter()
            logger.info(f"[STRM] 收尾缓存更新开始: {task_name} | 条目:{len(current_run_items)} | 跳过常驻索引重建")
            save_task_snapshot(
                task_key,
                current_run_items,
                meta={
                    "last_status": "finished",
                    "updated_at": time.time(),
                },
                rebuild_resident_index=False,
            )
            logger.info(f"[STRM] 收尾缓存更新完成: {task_name} | 耗时 {perf_counter() - finish_cache_start:.1f}s")
            _update_progress(run_id, f"STRM全量同步完成: {task_name}", 100, "finished", detail=stats.copy())
            logger.info(f"[STRM] 全量同步完成: {task_name} | 耗时 {elapsed:.1f}s | 扫描:{stats['scanned']} 文件夹数量:{stats['scanned_dirs']} 生成:{stats['generated']} 下载:{stats['downloaded']} TMDb补齐:{stats['tmdb_generated']} TMDb跳过:{stats['tmdb_skipped']} TMDb失败:{stats['tmdb_failed']} 跳过:{stats['skipped']} 删除:{stats['deleted']} 失败:{stats['failed']} 重试成功:{stats['retry_success']} 重试失败:{stats['retry_failed']}")
            _notify_task("success", "", stats.copy(), elapsed)

        except Exception as e:
            logger.error(f"[STRM] 全量同步异常: {e}")
            _update_progress(run_id, f"STRM: {task_name} (错误)", 100, "error")
            stats_payload = locals().get("stats") if isinstance(locals().get("stats"), dict) else None
            _notify_task("error", str(e), stats_payload)


# 模块级单例
strm_service = StrmService()


def generate_strm_for_file(pickcode: str, filename: str, remote_file_path: str):
    """整理完成后，为单个文件生成 strm（复用 strm 任务配置）"""
    try:
        result = strm_service.process_incremental_items([
            {
                "name": filename,
                "path": remote_file_path,
                "pickcode": pickcode,
                "size": 0,
                "id": 0,
                "sha1": "",
                "is_dir": False,
            }
        ])
        if result.get("matched_tasks"):
            logger.info(f"[STRM] 整理生成 strm 已提交增量通道: {remote_file_path}")
    except Exception as e:
        logger.error(f"[STRM] 整理生成 strm 失败: {e}")
