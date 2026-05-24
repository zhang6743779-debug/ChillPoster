"""
Scrape / STRM helpers for the media-organize workflow.

Extracted from app/routers/media_organize.py so that both the router
and any other service modules can import them without circular-dependency
issues.
"""

import os
import errno
import re
import json
import time as _time
from time import perf_counter
import threading
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter

from core.logger import logger
from app.services import media_organize_state as _state
from app.services.media_organize_state import (
    _target_event_lock,
    CONFIG_FILE,
)
from app.services.media_organize_tmdb import (
    _parse_filename,
    _search_tmdb_for_title_sync,
    _fetch_tmdb_data_sync,
    _load_config_data,
    _build_scraping_config,
)


# ---------------------------------------------------------------------------
# Utility / stub helpers (used by MediaOrganizer callbacks)
# ---------------------------------------------------------------------------

_FD_EXHAUSTED_ERRNOS = {errno.EMFILE, errno.ENFILE}
_FD_RETRY_MAX = 3
_FD_RETRY_BASE_SLEEP = 1.0

_TMDB_IMAGE_DOWNLOAD_SEMAPHORE = threading.Semaphore(40)
_TMDB_IMAGE_DOWNLOAD_TIMEOUT = (5.05, 20)
_TMDB_IMAGE_DOWNLOAD_RETRIES = 3
_IMAGE_SESSION_LOCAL = threading.local()
_METADATA_PROGRESS_LOG_LOCK = threading.Lock()
_METADATA_PROGRESS_LOG_COUNTS: dict[str, int] = {}
_METADATA_DONE_LOG_TIMES: dict[str, float] = {}
_METADATA_DONE_LOG_TTL_SECONDS = 3600.0

_bulk_mode_local = threading.local()


def set_bulk_mode(enabled: bool):
    _bulk_mode_local.active = enabled


def _is_bulk_mode() -> bool:
    return bool(getattr(_bulk_mode_local, "active", False))


def _begin_metadata_progress_log(title_for_log: str):
    if _is_bulk_mode():
        return
    title = str(title_for_log or "未知标题")
    with _METADATA_PROGRESS_LOG_LOCK:
        count = int(_METADATA_PROGRESS_LOG_COUNTS.get(title, 0) or 0) + 1
        _METADATA_PROGRESS_LOG_COUNTS[title] = count


def _end_metadata_progress_log(title_for_log: str):
    if _is_bulk_mode():
        return
    title = str(title_for_log or "未知标题")
    with _METADATA_PROGRESS_LOG_LOCK:
        current = int(_METADATA_PROGRESS_LOG_COUNTS.get(title, 0) or 0)
        if current <= 1:
            _METADATA_PROGRESS_LOG_COUNTS.pop(title, None)
        else:
            _METADATA_PROGRESS_LOG_COUNTS[title] = current - 1


def _metadata_done_label(tmdb_data: Optional[dict] = None, folder_name: str = "", fallback_path: str = "") -> str:
    label = str(folder_name or "").strip()
    if not label and fallback_path:
        path = Path(str(fallback_path))
        label = path.parent.name or path.stem
    if label:
        return label

    data = tmdb_data or {}
    title = str(data.get("title") or data.get("name") or "").strip()
    year = str(data.get("release_date") or data.get("first_air_date") or "")[:4]
    tmdb_id = str(data.get("id") or "").strip()
    if title and year and tmdb_id:
        return f"{title} ({year}) {{tmdb-{tmdb_id}}}"
    if title and year:
        return f"{title} ({year})"
    return title or "未知标题"


def _log_metadata_download_done(label: str):
    normalized_label = str(label or "未知标题").strip() or "未知标题"
    now = _time.monotonic()
    with _METADATA_PROGRESS_LOG_LOCK:
        if len(_METADATA_DONE_LOG_TIMES) > 2000:
            expired_before = now - _METADATA_DONE_LOG_TTL_SECONDS
            for key, logged_at in list(_METADATA_DONE_LOG_TIMES.items()):
                if logged_at < expired_before:
                    _METADATA_DONE_LOG_TIMES.pop(key, None)
        last_logged_at = float(_METADATA_DONE_LOG_TIMES.get(normalized_label, 0.0) or 0.0)
        should_log_info = not last_logged_at or now - last_logged_at >= _METADATA_DONE_LOG_TTL_SECONDS
        if should_log_info:
            _METADATA_DONE_LOG_TIMES[normalized_label] = now

    if should_log_info:
        logger.info(f"[MediaOrganize] 元数据下载完成: {normalized_label}")
    else:
        logger.debug(f"[MediaOrganize] 元数据下载完成，同一部剧已提示: {normalized_label}")


def _noop_transfer(src: str, dst: str):
    pass


def _atomic_write_bytes(path: str, content: bytes):
    target = str(path or "")
    if not target:
        return
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{target}.{threading.get_ident()}.{_time.time_ns()}.tmp"
    for fd_attempt in range(_FD_RETRY_MAX + 1):
        try:
            with open(tmp_path, 'wb') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target)
            return
        except OSError as e:
            if e.errno in _FD_EXHAUSTED_ERRNOS and fd_attempt < _FD_RETRY_MAX:
                wait = _FD_RETRY_BASE_SLEEP * (2 ** fd_attempt)
                logger.warning(f"[MediaOrganize] 文件句柄耗尽 (errno={e.errno})，等待 {wait:.1f}s 后重试: {target}")
                _time.sleep(wait)
                continue
            raise
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


def _write_nfo(path: str, content: str):
    _atomic_write_bytes(path, str(content or "").encode('utf-8'))


def _get_image_session():
    session = getattr(_IMAGE_SESSION_LOCAL, "session", None)
    if session is None:
        import requests
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        _IMAGE_SESSION_LOCAL.session = session
    return session


def _download_image(url: str, path: str):
    from core.configs import global_config
    global_config.load()
    started_at = perf_counter()
    proxies = None
    if global_config.proxy_url:
        proxies = {"http": global_config.proxy_url, "https": global_config.proxy_url}
    last_error = None
    logger.trace(f"[MediaOrganize] 图片下载开始: {path}")
    for attempt in range(_TMDB_IMAGE_DOWNLOAD_RETRIES + 1):
        try:
            with _TMDB_IMAGE_DOWNLOAD_SEMAPHORE:
                resp = _get_image_session().get(url, timeout=_TMDB_IMAGE_DOWNLOAD_TIMEOUT, proxies=proxies)
            resp.raise_for_status()
            _atomic_write_bytes(path, resp.content)
            logger.trace(f"[MediaOrganize] 图片下载完成: {path} | 大小:{len(resp.content)} | 尝试:{attempt + 1} | 耗时:{perf_counter() - started_at:.2f}s")
            return
        except Exception as e:
            last_error = e
            if attempt < _TMDB_IMAGE_DOWNLOAD_RETRIES:
                logger.trace(f"[MediaOrganize] 图片下载重试: {path} | 第{attempt + 1}次失败:{e}")
                continue
            break
    logger.warning(f"[MediaOrganize] 图片下载失败 {url}: {last_error} | 路径:{path} | 耗时:{perf_counter() - started_at:.2f}s")
    raise RuntimeError(f"图片下载失败: {last_error}")


# ---------------------------------------------------------------------------
# 115 upload scrape
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Local STRM scrape (Emby 免刮削)
# ---------------------------------------------------------------------------

def _scrape_to_strm_local(tmdb_data, media_type, target_name, target_folder,
                           scraping_config, overwrite, season_number=None,
                           episode_number=None, nfo_stem=None, category_path=""):
    """生成元数据文件并写入strm同步任务的本地目录（不上传115）"""
    title_for_log = (tmdb_data or {}).get("title") or (tmdb_data or {}).get("name") or target_folder or "未知标题"
    _begin_metadata_progress_log(title_for_log)
    try:
        cfg_path = "config/strm_config.json"
        if not os.path.exists(cfg_path):
            return []
        with open(cfg_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        tasks = cfg.get("sync_tasks", [])

        matched_local = None
        for task in tasks:
            remote_path = task.get("remote_path", "").rstrip("/")
            local_path = task.get("local_path", "")
            if not remote_path or not local_path:
                continue
            # 匹配：remote_path 末尾部分包含 target_name
            if target_name and target_name in remote_path:
                matched_local = local_path
                break

        if not matched_local:
            logger.warning(f"[MediaOrganize] Emby免刮削: 未找到匹配的strm任务 target_name={target_name}")
            return []

        from core.organizer import MediaOrganizer, MediaType

        dest_dir = os.path.join(matched_local, category_path or "", target_folder or "")
        series_dir = dest_dir
        if media_type in ("episode", "season") and season_number is not None:
            dest_dir = os.path.join(dest_dir, f"Season {season_number:02d}")
        os.makedirs(dest_dir, exist_ok=True)

        organizer = MediaOrganizer(
            library_root=dest_dir,
            transfer_media=_noop_transfer,
            save_nfo=_write_nfo,
            download_image=_download_image,
            scraping_config=scraping_config,
        )
        mt = MediaType(media_type)
        organizer.scrape_directory(
            dir_path=Path(dest_dir),
            tmdb_data=tmdb_data,
            media_type=mt,
            season_number=season_number,
            episode_number=episode_number,
            init_folder=True,
            season_image_dir=Path(series_dir) if media_type == "season" else None,
            recursive=False,
            overwrite=overwrite,
            video_stem=nfo_stem,
        )
        generated = [f for _, _, files in os.walk(dest_dir) for f in files]
        return generated
    except Exception as e:
        logger.error(f"[MediaOrganize] Emby免刮削失败: {e}")
        return []
    finally:
        _end_metadata_progress_log(title_for_log)


# ---------------------------------------------------------------------------
# Metadata task processor (serial queue)
# ---------------------------------------------------------------------------

def _process_metadata_task(config_data: dict, meta_ctx: dict) -> list:
    """元数据任务：串行队列中执行，避免阻塞 rename/move 主流程"""
    generated = []
    if not meta_ctx:
        return generated

    if not config_data.get("emby_local_scrape", False):
        return generated

    tmdb_data = meta_ctx.get("tmdb_data")
    media_type = meta_ctx.get("media_type")
    overwrite = meta_ctx.get("overwrite", False)
    scraping_config = _build_scraping_config(config_data)

    if media_type == "movie":
        generated += _scrape_to_strm_local(
            tmdb_data, "movie",
            config_data.get("target_name", ""), meta_ctx.get("folder_name", ""),
            scraping_config, overwrite,
            category_path=meta_ctx.get("category_path", ""),
            nfo_stem=meta_ctx.get("nfo_stem", ""),
        )
        if generated:
            _log_metadata_download_done(_metadata_done_label(tmdb_data, meta_ctx.get("folder_name", "")))
        return generated

    if media_type == "tv":
        season_cid = str(meta_ctx.get("season_cid", ""))
        season_num = meta_ctx.get("season_number")
        episode_num = meta_ctx.get("episode_number")
        folder_name = meta_ctx.get("folder_name", "")
        category_path = meta_ctx.get("category_path", "")

        if meta_ctx.get("scrape_tv_root", False):
            generated += _scrape_to_strm_local(
                tmdb_data, "tv",
                config_data.get("target_name", ""), folder_name,
                scraping_config, overwrite,
                category_path=category_path,
            )

        if season_cid and meta_ctx.get("scrape_season", False):
            generated += _scrape_to_strm_local(
                tmdb_data, "season",
                config_data.get("target_name", ""), folder_name,
                scraping_config, overwrite,
                season_number=season_num,
                category_path=category_path,
            )

        if season_cid and episode_num is not None:
            generated += _scrape_to_strm_local(
                tmdb_data, "episode",
                config_data.get("target_name", ""), folder_name,
                scraping_config, overwrite,
                season_number=season_num,
                episode_number=episode_num,
                nfo_stem=meta_ctx.get("nfo_stem", ""),
                category_path=category_path,
            )

        if generated:
            _log_metadata_download_done(_metadata_done_label(tmdb_data, folder_name))

    return generated


# ---------------------------------------------------------------------------
# STRM generation helpers
# ---------------------------------------------------------------------------

def _build_strm_items_on_organize(result: dict, media_type: str, config_data: dict, pickcode: str = "", category_path: str = "") -> tuple[list[dict], str]:
    if not pickcode:
        return [], ""

    target_name = config_data.get("target_name", "")
    if not target_name or target_name == "根目录":
        return [], ""

    renamed_file = result.get("renamed_file", "")
    target_folder = result.get("target_folder", "")
    if not renamed_file or not target_folder:
        return [], ""

    cat = f"/{category_path}" if category_path and category_path != "其他" else ""
    if media_type == "movie":
        remote_file_path = f"{target_name}{cat}/{target_folder}/{renamed_file}"
    else:
        season_dir = result.get("season_dir", "")
        remote_file_path = f"{target_name}{cat}/{target_folder}/{season_dir}/{renamed_file}"

    items = [{
        "name": renamed_file,
        "path": remote_file_path,
        "pickcode": pickcode,
        "size": int(result.get("size", 0) or 0),
        "id": int(result.get("id", 0) or 0),
        "sha1": str(result.get("sha1", "") or ""),
        "is_dir": False,
    }]

    remote_dir = os.path.dirname(remote_file_path)
    for sub in result.get("moved_subtitles", []) or []:
        sub_name = str(sub.get("name", "") or "")
        sub_pickcode = str(sub.get("pickcode", "") or "")
        if not sub_name or not sub_pickcode:
            continue
        items.append({
            "name": sub_name,
            "path": f"{remote_dir}/{sub_name}",
            "pickcode": sub_pickcode,
            "size": int(sub.get("size", 0) or 0),
            "id": int(sub.get("id", 0) or 0),
            "sha1": str(sub.get("sha1", "") or ""),
            "is_dir": False,
        })

    return items, remote_file_path.rstrip("/")


def _mark_recent_organize_strm_paths(paths: list[str]):
    valid_paths = [str(p or "").rstrip("/") for p in (paths or []) if str(p or "").rstrip("/")]
    if not valid_paths:
        return
    now = _time.time()
    for remote_file_path in valid_paths:
        _state._recent_organize_strm_paths[remote_file_path] = now
    if len(_state._recent_organize_strm_paths) > 3000:
        _state._recent_organize_strm_paths = {
            p: ts for p, ts in _state._recent_organize_strm_paths.items()
            if now - ts <= 600
        }


def _generate_strm_on_organize(result: dict, media_type: str, config_data: dict, pickcode: str = "", category_path: str = ""):
    """整理完成后，调用 strm 服务增量生成 strm，并下载字幕文件到本地。返回实际生成的 strm 数量。"""
    try:
        from app.services.strm_service import strm_service

        items, remote_file_path = _build_strm_items_on_organize(
            result, media_type, config_data, pickcode=pickcode, category_path=category_path
        )
        if not items:
            return 0
        for item in items:
            item["_skip_tmdb_metadata"] = True

        inc_result = strm_service.process_incremental_items(items)
        if not inc_result.get("matched_tasks"):
            return 0

        generated_count = int(inc_result.get("generated", 0) or 0)
        if generated_count == 0:
            skip_reasons = inc_result.get("skip_reasons") or {}
            if skip_reasons:
                logger.info(f"[MediaOrganize] STRM 未生成，跳过原因: {skip_reasons}")
        if generated_count > 0:
            _mark_recent_organize_strm_paths([remote_file_path])

        return generated_count

    except Exception as e:
        logger.error(f"[MediaOrganize] 生成 strm 失败: {e}")
        return 0


def _generate_strm_batch_on_organize(payloads: list[dict], config_data: dict, cancel_event: Optional[threading.Event] = None) -> int:
    """整理完成后，批量调用 strm 服务增量生成 strm，并下载字幕文件到本地。"""
    try:
        from app.services.strm_service import strm_service

        if cancel_event and cancel_event.is_set():
            return 0

        items: list[dict] = []
        remote_file_paths: list[str] = []
        replace_remote_paths: list[str] = []
        for payload in payloads or []:
            if cancel_event and cancel_event.is_set():
                logger.info("[MediaOrganize] STRM 批量生成收到取消信号，停止构建载荷")
                return 0
            force_overwrite = bool(payload.get("force_overwrite"))
            batch_items, remote_file_path = _build_strm_items_on_organize(
                payload.get("result", {}),
                payload.get("media_type", ""),
                config_data,
                pickcode=str(payload.get("pickcode", "") or ""),
                category_path=str(payload.get("category_path", "") or ""),
            )
            if not batch_items:
                continue
            if force_overwrite:
                for item in batch_items:
                    item["_force_strm_overwrite"] = True
            for item in batch_items:
                item["_skip_tmdb_metadata"] = True
            replace_remote_paths.extend([
                str(p or "").rstrip("/")
                for p in (payload.get("replace_remote_paths") or [])
                if str(p or "").rstrip("/")
            ])
            items.extend(batch_items)
            if remote_file_path:
                remote_file_paths.append(remote_file_path)

        if not items:
            return 0

        for remote_path in dict.fromkeys(replace_remote_paths):
            if cancel_event and cancel_event.is_set():
                return 0
            cleanup_result = strm_service.remove_local_strm_for_remote_file(
                remote_path,
                reason="媒体整理洗版替换旧版本",
            )
            logger.info(f"[MediaOrganize] 洗版替换已清理旧STRM: {cleanup_result}")

        inc_result = strm_service.process_incremental_items(items, cancel_event=cancel_event)
        generated_count = int(inc_result.get("generated", 0) or 0)
        if inc_result.get("status") == "cancelled":
            logger.info(f"[MediaOrganize] STRM 批量生成已取消，已生成: {generated_count}")
        if not inc_result.get("matched_tasks"):
            return generated_count

        if generated_count == 0:
            skip_reasons = inc_result.get("skip_reasons") or {}
            if skip_reasons:
                logger.info(f"[MediaOrganize] STRM 批量未生成，跳过原因: {skip_reasons}")
        if generated_count > 0:
            _mark_recent_organize_strm_paths(remote_file_paths)

        return generated_count

    except Exception as e:
        logger.error(f"[MediaOrganize] 批量生成 strm 失败: {e}")
        return 0


# ---------------------------------------------------------------------------
# Target folder / category derivation
# ---------------------------------------------------------------------------

def _derive_target_folder_and_category(remote_file_path: str, target_dir: str) -> tuple[str, str]:
    rel = remote_file_path[len(target_dir.rstrip("/")):].lstrip("/")
    parts = [p for p in rel.split("/") if p]
    if len(parts) < 2:
        return "", ""

    season_idx = next((i for i, p in enumerate(parts) if re.match(r"(?i)^season\s*\d+$", p)), None)
    if season_idx is not None and season_idx >= 1:
        target_folder = parts[season_idx - 1]
        category_path = "/".join(parts[:season_idx - 1])
        return target_folder, category_path

    target_folder = parts[-2]
    category_path = "/".join(parts[:-2])
    return target_folder, category_path


# ---------------------------------------------------------------------------
# Recent-organize dedup check
# ---------------------------------------------------------------------------

def _is_recent_organize_generated(remote_file_path: str) -> bool:
    now = _time.time()
    with _target_event_lock:
        for p, ts in list(_state._recent_organize_strm_paths.items()):
            if now - ts > 120:
                _state._recent_organize_strm_paths.pop(p, None)
        return remote_file_path in _state._recent_organize_strm_paths


# ---------------------------------------------------------------------------
# Event-triggered STRM / metadata generation
# ---------------------------------------------------------------------------

def _generate_strm_for_event(pickcode: str, file_name: str, remote_file_path: str) -> bool:
    """同步函数：为事件触发的单个文件生成 STRM（在线程池中执行）"""
    try:
        from app.services.strm_service import strm_service
        if not pickcode:
            return False
        result = strm_service.process_incremental_items([
            {
                "name": file_name,
                "path": remote_file_path,
                "pickcode": pickcode,
                "size": 0,
                "id": 0,
                "sha1": "",
                "is_dir": False,
            }
        ])
        return bool(result.get("matched_tasks"))
    except Exception as e:
        logger.error(f"[115Life] 事件 STRM 生成失败 {file_name}: {e}")
        return False


def _scrape_event_metadata(
    file_name: str, remote_file_path: str, target_dir: str,
    target_name: str, scraping_config: dict, cfg: dict,
) -> bool:
    """同步函数：TMDb 识别 + 本地元数据刮削（在线程池中执行）"""
    try:
        parsed = _parse_filename(file_name, file_path=remote_file_path)
        if not parsed:
            return False

        from core.configs import global_config
        global_config.load()
        api_key = global_config.tmdb_key
        if not api_key:
            return False

        # TMDb 搜索（同步 requests，在线程池中不阻塞主循环）
        search_result = _search_tmdb_for_title_sync(parsed, api_key, set())
        if not search_result:
            return False

        resolved_season = search_result.get("season", parsed.get("season"))
        resolved_episode = search_result.get("episode", parsed.get("episode"))

        tmdb_data = _fetch_tmdb_data_sync(
            search_result["tmdb_id"], parsed["media_type"], api_key, resolved_season, parsed,
        )
        if not tmdb_data:
            return False

        target_folder, category_path = _derive_target_folder_and_category(remote_file_path, target_dir)
        if not target_folder:
            return False

        generated = _scrape_to_strm_local(
            tmdb_data=tmdb_data,
            media_type="movie" if parsed["media_type"] == "movie" else "episode",
            target_name=target_name,
            target_folder=target_folder,
            scraping_config=scraping_config,
            overwrite=False,
            season_number=resolved_season,
            episode_number=resolved_episode,
            nfo_stem=os.path.splitext(file_name)[0],
            category_path=category_path,
        )
        if generated:
            _log_metadata_download_done(_metadata_done_label(tmdb_data, target_folder))
        return True
    except Exception as e:
        logger.error(f"[115Life] 事件元数据刮削失败 {file_name}: {e}")
        return False


def _map_remote_to_strm_local_path(remote_path: str) -> str:
    """将 115 远端路径映射为 STRM 本地路径，用于精确匹配 Emby 库路径"""
    try:
        cfg_path = "config/strm_config.json"
        if not os.path.exists(cfg_path):
            return remote_path
        with open(cfg_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        tasks = cfg.get("sync_tasks", [])

        for task in tasks:
            remote_root = str(task.get("remote_path", "")).rstrip("/")
            local_root = str(task.get("local_path", "")).rstrip("/")
            if not remote_root or not local_root:
                continue
            if remote_path == remote_root or remote_path.startswith(remote_root + "/"):
                rel = remote_path[len(remote_root):].lstrip("/")
                mapped = os.path.join(local_root, rel) if rel else local_root
                return str(Path(mapped).resolve())
    except Exception as e:
        logger.debug(f"[MediaOrganize] 刷新路径映射失败: {e}")

    return remote_path


def _is_season_dir_name(name: str) -> bool:
    return bool(re.match(r"(?i)^season\s*\d+$", str(name or "").strip()))


def _derive_strm_tv_dirs(local_file_path: str) -> tuple[Path, Path]:
    season_dir = Path(local_file_path).parent
    if _is_season_dir_name(season_dir.name):
        return season_dir.parent, season_dir
    return season_dir, season_dir


def _collect_expected_tmdb_metadata_paths(local_file_path: str, media_type: str, season_number: Optional[int] = None, episode_number: Optional[int] = None) -> dict:
    local_file = Path(local_file_path)
    file_stem = local_file.stem

    if media_type == "movie":
        movie_dir = local_file.parent
        return {
            "movie_nfo": movie_dir / f"{file_stem}.nfo",
            "movie_poster": movie_dir / "poster.jpg",
            "movie_fanart": movie_dir / "fanart.jpg",
            "movie_logo": movie_dir / "logo.png",
            "movie_disc": movie_dir / "disc.png",
            "movie_banner": movie_dir / "banner.jpg",
            "movie_thumb": movie_dir / "thumb.jpg",
        }

    series_dir, season_dir = _derive_strm_tv_dirs(local_file_path)
    targets = {
        "tv_nfo": series_dir / "tvshow.nfo",
        "tv_poster": series_dir / "poster.jpg",
        "tv_fanart": series_dir / "fanart.jpg",
        "tv_logo": series_dir / "logo.png",
        "tv_banner": series_dir / "banner.jpg",
        "tv_thumb": series_dir / "thumb.jpg",
    }

    if season_number is not None:
        targets.update({
            "season_nfo": season_dir / "season.nfo",
            "season_poster": series_dir / f"season{int(season_number):02d}-poster.jpg",
        })

    if episode_number is not None:
        targets.update({
            "episode_nfo": season_dir / f"{file_stem}.nfo",
            "episode_thumb": season_dir / f"{file_stem}-thumb.jpg",
        })

    return targets


def list_missing_tmdb_metadata_for_strm(local_file_path: str, media_type: str, scraping_config, season_number: Optional[int] = None, episode_number: Optional[int] = None) -> list[str]:
    from core.organizer import MediaType, MetadataType

    targets = _collect_expected_tmdb_metadata_paths(local_file_path, media_type, season_number, episode_number)
    missing: list[str] = []

    def _want(mt, md) -> bool:
        return scraping_config.get_policy(mt, md).value != "skip"

    checks = []
    if media_type == "movie":
        checks.extend([
            (MediaType.MOVIE, MetadataType.NFO, "movie_nfo"),
            (MediaType.MOVIE, MetadataType.POSTER, "movie_poster"),
            (MediaType.MOVIE, MetadataType.BACKDROP, "movie_fanart"),
            (MediaType.MOVIE, MetadataType.LOGO, "movie_logo"),
            (MediaType.MOVIE, MetadataType.DISC, "movie_disc"),
            (MediaType.MOVIE, MetadataType.BANNER, "movie_banner"),
            (MediaType.MOVIE, MetadataType.THUMB, "movie_thumb"),
        ])
    else:
        checks.extend([
            (MediaType.TV, MetadataType.NFO, "tv_nfo"),
            (MediaType.TV, MetadataType.POSTER, "tv_poster"),
            (MediaType.TV, MetadataType.BACKDROP, "tv_fanart"),
            (MediaType.TV, MetadataType.LOGO, "tv_logo"),
            (MediaType.TV, MetadataType.BANNER, "tv_banner"),
            (MediaType.TV, MetadataType.THUMB, "tv_thumb"),
        ])
        if season_number is not None:
            checks.extend([
                (MediaType.SEASON, MetadataType.NFO, "season_nfo"),
                (MediaType.SEASON, MetadataType.POSTER, "season_poster"),
            ])
        if episode_number is not None:
            checks.extend([
                (MediaType.EPISODE, MetadataType.NFO, "episode_nfo"),
                (MediaType.EPISODE, MetadataType.THUMB, "episode_thumb"),
            ])

    for media_enum, meta_enum, key in checks:
        path = targets.get(key)
        if not path:
            continue
        if not _want(media_enum, meta_enum):
            continue
        if not path.exists():
            missing.append(key)

    return missing


def scrape_tmdb_metadata_for_strm_local_file(local_file_path: str, tmdb_data: dict, media_type: str, scraping_config, season_number: Optional[int] = None, episode_number: Optional[int] = None, overwrite: bool = False) -> list[str]:
    from core.organizer import MediaOrganizer, MediaType

    started_at = perf_counter()
    local_file = Path(local_file_path)
    if not local_file_path:
        return []

    logger.trace(
        f"[MediaOrganize] STRM本地元数据刮削开始: {local_file_path} | 类型:{media_type} | "
        f"季:{season_number} 集:{episode_number}"
    )
    generated: list[str] = []
    organizer = MediaOrganizer(
        library_root=str(local_file.parent),
        transfer_media=_noop_transfer,
        save_nfo=_write_nfo,
        download_image=_download_image,
        scraping_config=scraping_config,
    )

    if media_type == "movie":
        movie_started = perf_counter()
        result = organizer.scrape_directory(
            dir_path=local_file.parent,
            tmdb_data=tmdb_data,
            media_type=MediaType.MOVIE,
            init_folder=True,
            recursive=False,
            overwrite=overwrite,
            video_stem=local_file.stem,
        )
        generated = result.metadata_files if result.success else []
        logger.trace(
            f"[MediaOrganize] STRM电影元数据刮削完成: {local_file_path} | 生成:{len(generated)} | "
            f"耗时:{perf_counter() - movie_started:.2f}s | 总耗时:{perf_counter() - started_at:.2f}s"
        )
        if generated:
            _log_metadata_download_done(_metadata_done_label(tmdb_data, fallback_path=local_file_path))
        return generated

    series_dir, season_dir = _derive_strm_tv_dirs(local_file_path)
    root_started = perf_counter()
    root_result = organizer.scrape_directory(
        dir_path=series_dir,
        tmdb_data=tmdb_data,
        media_type=MediaType.TV,
        init_folder=True,
        recursive=False,
        overwrite=overwrite,
    )
    if root_result.success:
        generated.extend(root_result.metadata_files)
    logger.trace(
        f"[MediaOrganize] STRM剧集根元数据刮削完成: {series_dir} | 生成:{len(root_result.metadata_files) if root_result.success else 0} | "
        f"耗时:{perf_counter() - root_started:.2f}s"
    )

    if season_number is not None:
        season_started = perf_counter()
        season_result = organizer.scrape_directory(
            dir_path=season_dir,
            tmdb_data=tmdb_data,
            media_type=MediaType.SEASON,
            season_number=season_number,
            init_folder=True,
            recursive=False,
            overwrite=overwrite,
            season_image_dir=series_dir,
        )
        if season_result.success:
            generated.extend(season_result.metadata_files)
        logger.trace(
            f"[MediaOrganize] STRM季元数据刮削完成: {season_dir} | 生成:{len(season_result.metadata_files) if season_result.success else 0} | "
            f"耗时:{perf_counter() - season_started:.2f}s"
        )

    if episode_number is not None:
        episode_started = perf_counter()
        episode_result = organizer.scrape_directory(
            dir_path=season_dir,
            tmdb_data=tmdb_data,
            media_type=MediaType.EPISODE,
            season_number=season_number or 0,
            episode_number=episode_number,
            init_folder=True,
            recursive=False,
            overwrite=overwrite,
            video_stem=local_file.stem,
        )
        if episode_result.success:
            generated.extend(episode_result.metadata_files)
        logger.trace(
            f"[MediaOrganize] STRM单集元数据刮削完成: {local_file_path} | 生成:{len(episode_result.metadata_files) if episode_result.success else 0} | "
            f"耗时:{perf_counter() - episode_started:.2f}s"
        )

    logger.trace(
        f"[MediaOrganize] STRM本地元数据刮削完成: {local_file_path} | 生成:{len(generated)} | "
        f"总耗时:{perf_counter() - started_at:.2f}s"
    )
    if generated:
        _log_metadata_download_done(_metadata_done_label(tmdb_data, fallback_path=local_file_path))
    return generated
