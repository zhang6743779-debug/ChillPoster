"""
Media organize core logic extracted from app/routers/media_organize.py.

Contains the master orchestrator and supporting functions for:
- Media file organization (movie + TV)
- Life event callbacks for auto-organize
- Target event queue processing
- Source directory cleanup
"""

import os
import re
import json
import uuid
import asyncio
import time as _time
import threading
import hashlib
from datetime import datetime
from typing import Optional, List, Callable
from concurrent.futures import ThreadPoolExecutor

from app.services import media_organize_state as _state
from app.services.media_organize_state import (
    _organize_trigger_lock, _target_event_lock, _target_event_queue,
    _source_poll_lock, _read_lock,
    VIDEO_EXTS, SUBTITLE_EXTS, CONFIG_FILE,
    _record_organized_source_path, _is_self_organized_event,
)
from app.services.strm_service import (
    classify_file,
    DEFAULT_AUDIO_EXTS,
    DEFAULT_IMAGE_EXTS,
    DEFAULT_DATA_EXTS,
)
from app.services.media_organize_115_ops import (
    _get_115_client, _get_115_fs, _list_115_tree_entries, _iter_115_media_entries, _iter_115_media_entries_from_tree,
    _rename_115_file, _rename_115_files_batch, _match_and_move_subtitles, _move_top_dir_to_failed, _move_failed_files_batch,
    _move_matched_subtitles_to_target, _match_and_move_subtitles_batch, _ensure_115_dir_chain_cached,
    _collect_event_video_sha1s_for_cache, _mkdir_115_dir, _move_115_items, _run_115_write_request_sync,
    _get_115_direct_url, _get_115_direct_urls,
)
from app.services.media_organize_tmdb import (
    _parse_filename, _search_tmdb_for_title, _fetch_tmdb_data,
    _load_config_data, _build_scraping_config,
)
from app.services.media_organize_template import (
    _build_template_variables, _render_template,
)
from app.services.media_organize_scrape import (
    _process_metadata_task, _generate_strm_batch_on_organize,
    _scrape_event_metadata, _generate_strm_for_event,
    _is_recent_organize_generated, _scrape_to_strm_local,
    _map_remote_to_strm_local_path,
)
from app.services.media_server_refresh import media_server_refresh
from app.dependencies import ACTIVE_TASKS, update_task_progress
from core.logger import logger
from core.media_library_cache import build_task_key, get_task_index, get_task_item_by_id, get_task_items, remove_items_by_path_prefix, remove_task_item_by_id, update_items_path_prefix, update_task_item_fields, upsert_task_item, upsert_dir_item, merge_task_items
from core.meta.mediainfo import extract_wash_fields


class _OrganizeCancelledError(Exception):
    pass


_WASH_CODEC_MULTIPLIERS = {
    "H264": 1.0,
    "H265": 1.6,
    "AV1": 1.7,
    "MPEG4": 0.6,
    "XVID": 0.6,
    "DIVX": 0.6,
}
_FFPROBE_FILE_CACHE_PATH = "config/media_organize_ffprobe_cache.json"
_FFPROBE_BATCH_CACHE_PATH = "config/media_organize_ffprobe_batch_cache.json"
_FFPROBE_BATCH_CACHE_TTL_SECONDS = 1800
_FFPROBE_BATCH_SAMPLE_LIMIT = 3
_FFPROBE_PROFILE_FIELDS = (
    "resource_pix",
    "video_encode",
    "audio_encode",
    "fps",
    "resource_effect",
    "video_effect",
    "color_depth",
    "source",
    "release_group",
)
_FFPROBE_CACHE_LOCK = threading.Lock()
_FFPROBE_FILE_CACHE: Optional[dict] = None
_FFPROBE_BATCH_CACHE: Optional[dict] = None
_FFPROBE_MEDIA_DOWNLOAD_LIMIT = 3
_FFPROBE_MEDIA_GATE = threading.BoundedSemaphore(_FFPROBE_MEDIA_DOWNLOAD_LIMIT)
_FFPROBE_EXEC_TIMEOUT_SECONDS = 45


def _load_ffprobe_file_cache() -> dict:
    global _FFPROBE_FILE_CACHE
    with _FFPROBE_CACHE_LOCK:
        if _FFPROBE_FILE_CACHE is not None:
            return _FFPROBE_FILE_CACHE
        if os.path.exists(_FFPROBE_FILE_CACHE_PATH):
            try:
                with open(_FFPROBE_FILE_CACHE_PATH, "r", encoding="utf-8") as f:
                    _FFPROBE_FILE_CACHE = json.load(f)
            except Exception as e:
                logger.warning(f"[MediaOrganize] ffprobe缓存加载失败: {e}")
                _FFPROBE_FILE_CACHE = {}
        else:
            _FFPROBE_FILE_CACHE = {}
        return _FFPROBE_FILE_CACHE



def _save_ffprobe_file_cache() -> None:
    with _FFPROBE_CACHE_LOCK:
        cache = _FFPROBE_FILE_CACHE or {}
        os.makedirs(os.path.dirname(_FFPROBE_FILE_CACHE_PATH), exist_ok=True)
        with open(_FFPROBE_FILE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)



def _load_ffprobe_batch_cache() -> dict:
    global _FFPROBE_BATCH_CACHE
    now_ts = int(_time.time())
    with _FFPROBE_CACHE_LOCK:
        if _FFPROBE_BATCH_CACHE is None:
            if os.path.exists(_FFPROBE_BATCH_CACHE_PATH):
                try:
                    with open(_FFPROBE_BATCH_CACHE_PATH, "r", encoding="utf-8") as f:
                        _FFPROBE_BATCH_CACHE = json.load(f)
                except Exception as e:
                    logger.warning(f"[MediaOrganize] ffprobe批次缓存加载失败: {e}")
                    _FFPROBE_BATCH_CACHE = {}
            else:
                _FFPROBE_BATCH_CACHE = {}
        stale_keys = [
            key for key, value in (_FFPROBE_BATCH_CACHE or {}).items()
            if now_ts - int((value or {}).get("updated_at", 0) or 0) > _FFPROBE_BATCH_CACHE_TTL_SECONDS
        ]
        for key in stale_keys:
            _FFPROBE_BATCH_CACHE.pop(key, None)
        return _FFPROBE_BATCH_CACHE



def _save_ffprobe_batch_cache() -> None:
    with _FFPROBE_CACHE_LOCK:
        cache = _FFPROBE_BATCH_CACHE or {}
        os.makedirs(os.path.dirname(_FFPROBE_BATCH_CACHE_PATH), exist_ok=True)
        with open(_FFPROBE_BATCH_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)



def _build_ffprobe_file_cache_key(file_item: dict) -> str:
    sha1 = str((file_item or {}).get("sha1", "") or "").strip().upper()
    if sha1:
        return f"sha1:{sha1}"
    pickcode = str((file_item or {}).get("pickcode", "") or "").strip()
    size = int((file_item or {}).get("size", 0) or 0)
    if pickcode:
        return f"pickcode:{pickcode}:size:{size}"
    file_id = str((file_item or {}).get("id", "") or "").strip()
    if file_id:
        return f"id:{file_id}:size:{size}"
    return ""



def _normalize_ffprobe_fields(fields: dict) -> dict:
    if not fields:
        return {}
    return {
        "resource_pix": str(fields.get("resource_pix", "") or ""),
        "video_encode": str(fields.get("video_encode", "") or ""),
        "audio_encode": str(fields.get("audio_encode", "") or ""),
        "fps": str(fields.get("fps", "") or ""),
        "resource_effect": str(fields.get("resource_effect", "") or ""),
        "video_effect": str(fields.get("video_effect", "") or ""),
        "color_depth": str(fields.get("color_depth", "") or ""),
        "source": str(fields.get("source", "") or ""),
        "release_group": str(fields.get("release_group", "") or ""),
        "duration_seconds": float(fields.get("duration_seconds") or 0.0),
    }



def _get_cached_ffprobe_fields(file_item: dict) -> dict:
    cache_key = _build_ffprobe_file_cache_key(file_item)
    if not cache_key:
        return {}
    cache = _load_ffprobe_file_cache()
    return _normalize_ffprobe_fields(cache.get(cache_key) or {})



def _set_cached_ffprobe_fields(file_item: dict, fields: dict) -> None:
    global _FFPROBE_FILE_CACHE
    cache_key = _build_ffprobe_file_cache_key(file_item)
    normalized = _normalize_ffprobe_fields(fields)
    if not cache_key or not normalized:
        return
    with _FFPROBE_CACHE_LOCK:
        if _FFPROBE_FILE_CACHE is None:
            _FFPROBE_FILE_CACHE = {}
        _FFPROBE_FILE_CACHE[cache_key] = {
            **normalized,
            "updated_at": int(_time.time()),
            "size": int((file_item or {}).get("size", 0) or 0),
        }
    try:
        _save_ffprobe_file_cache()
    except Exception as e:
        logger.warning(f"[MediaOrganize] ffprobe缓存保存失败: {e}")



def _merge_probe_fields_into_variables(variables: dict, probe_fields: dict) -> dict:
    merged = dict(variables or {})
    for field in _FFPROBE_PROFILE_FIELDS:
        value = str((probe_fields or {}).get(field, "") or "")
        if value:
            merged[field] = value
    return merged



def _build_ffprobe_batch_key(tmdb_id: int, parsed: dict, file_item: dict, ext: str) -> tuple:
    meta_info = dict((parsed or {}).get("meta_info") or {})
    parent_dir = os.path.basename(os.path.dirname(str((file_item or {}).get("path", "") or "")))
    season = int((parsed or {}).get("season") or 0)
    return (
        str(tmdb_id or ""),
        season,
        str(parent_dir or ""),
        str(ext or "").lower(),
        str(meta_info.get("resource_pix", "") or ""),
        str(meta_info.get("video_encode", "") or ""),
        str(meta_info.get("resource_type", "") or ""),
        str(meta_info.get("resource_team", "") or ""),
    )



def _serialize_ffprobe_batch_key(batch_key: tuple) -> str:
    return "|".join(str(part or "") for part in batch_key)



def _get_cached_ffprobe_batch_profile(batch_key: tuple) -> dict:
    serialized_key = _serialize_ffprobe_batch_key(batch_key)
    cache = _load_ffprobe_batch_cache()
    entry = cache.get(serialized_key) or {}
    profile = _normalize_ffprobe_fields(entry.get("profile") or {})
    if not profile:
        return {}
    profile["ext"] = str((entry.get("profile") or {}).get("ext", "") or "")
    profile["sample_count"] = int((entry.get("profile") or {}).get("sample_count", 0) or 0)
    return profile



def _set_cached_ffprobe_batch_profile(batch_key: tuple, profile: dict) -> None:
    global _FFPROBE_BATCH_CACHE
    serialized_key = _serialize_ffprobe_batch_key(batch_key)
    normalized = _normalize_ffprobe_fields(profile)
    if not serialized_key or not normalized:
        return
    payload = {
        "profile": {
            **normalized,
            "ext": str((profile or {}).get("ext", "") or ""),
            "sample_count": int((profile or {}).get("sample_count", 0) or 0),
        },
        "updated_at": int(_time.time()),
    }
    with _FFPROBE_CACHE_LOCK:
        if _FFPROBE_BATCH_CACHE is None:
            _FFPROBE_BATCH_CACHE = {}
        _FFPROBE_BATCH_CACHE[serialized_key] = payload
    try:
        _save_ffprobe_batch_cache()
    except Exception as e:
        logger.warning(f"[MediaOrganize] ffprobe批次缓存保存失败: {e}")



def _is_special_probe_candidate(file_name: str) -> bool:
    name = str(file_name or "")
    return bool(re.search(r"(?i)(\bSP\b|\bOVA\b|\bNCOP\b|\bNCED\b|SPECIAL|特典|特别篇|特別篇)", name))



def _merge_ffprobe_profiles(base: dict, incoming: dict) -> dict:
    merged = dict(base or {})
    for field in _FFPROBE_PROFILE_FIELDS:
        value = str((incoming or {}).get(field, "") or "")
        if value:
            merged[field] = value
    merged["duration_seconds"] = float((incoming or {}).get("duration_seconds") or merged.get("duration_seconds") or 0.0)
    merged["ext"] = str((incoming or {}).get("ext", "") or merged.get("ext", "") or "")
    merged["sample_count"] = int((incoming or {}).get("sample_count", 0) or merged.get("sample_count") or 0)
    return merged



def _profiles_match_for_batch(base: dict, incoming: dict) -> bool:
    if not base or not incoming:
        return True
    compare_fields = ("resource_pix", "video_encode", "audio_encode", "fps", "source")
    for field in compare_fields:
        base_val = str((base or {}).get(field, "") or "")
        incoming_val = str((incoming or {}).get(field, "") or "")
        if base_val and incoming_val and base_val != incoming_val:
            return False
    return True



def _make_ffprobe_sample_profile(probe_fields: dict, ext: str, sample_count: int = 1) -> dict:
    return {
        **(probe_fields or {}),
        "ext": str(ext or "").lower(),
        "sample_count": int(sample_count or 1),
    }



def _is_ffprobe_size_anomaly_reason(reason: str) -> bool:
    return str(reason or "") in {"size_outlier", "size_outlier_strong"}



def _append_ffprobe_batch_size(batch_sizes: list[int], size: int) -> None:
    if int(size or 0) > 0:
        batch_sizes.append(int(size or 0))



def _record_ffprobe_segment_sample(segment_state: dict | None, sampled_profile: dict, size: int) -> tuple[dict, bool]:
    if not segment_state or not _profiles_match_for_batch(segment_state.get("profile") or {}, sampled_profile):
        return {
            "profile": _make_ffprobe_sample_profile(sampled_profile, sampled_profile.get("ext", ""), 1),
            "sizes": [int(size or 0)] if int(size or 0) > 0 else [],
        }, bool(segment_state)

    profile = segment_state.get("profile") or {}
    sample_count = int(profile.get("sample_count", 0) or 0)
    sizes = list(segment_state.get("sizes") or [])
    _append_ffprobe_batch_size(sizes, int(size or 0))
    return {
        "profile": _merge_ffprobe_profiles(profile, {**sampled_profile, "sample_count": sample_count + 1}),
        "sizes": sizes,
    }, False



def _is_ffprobe_anomaly(file_item: dict, parsed: dict, batch_profile: dict | None, batch_sizes: list[int]) -> tuple[bool, str]:
    if _is_special_probe_candidate(str((file_item or {}).get("name", "") or "")):
        return True, "special_episode"

    current_ext = os.path.splitext(str((file_item or {}).get("name", "") or ""))[1].lower()
    profile_ext = str((batch_profile or {}).get("ext", "") or "").lower()
    if profile_ext and current_ext and current_ext != profile_ext:
        return True, "extension_changed"

    parsed_meta = dict((parsed or {}).get("meta_info") or {})
    if batch_profile:
        profile_pix = str(batch_profile.get("resource_pix", "") or "")
        profile_venc = str(batch_profile.get("video_encode", "") or "")
        parsed_pix = str(parsed_meta.get("resource_pix", "") or "")
        parsed_venc = str(parsed_meta.get("video_encode", "") or "")
        if parsed_pix and profile_pix and parsed_pix != profile_pix:
            return True, "resolution_conflict"
        if parsed_venc and profile_venc and parsed_venc != profile_venc:
            return True, "codec_conflict"
        if not parsed_pix and not parsed_venc and int((batch_profile or {}).get("sample_count", 0) or 0) < _FFPROBE_BATCH_SAMPLE_LIMIT:
            return True, "need_more_samples"

    if batch_sizes:
        size = int((file_item or {}).get("size", 0) or 0)
        positives = sorted(v for v in batch_sizes if int(v or 0) > 0)
        if positives and size > 0:
            median_size = positives[len(positives) // 2]
            if median_size > 0:
                ratio = abs(size - median_size) / float(median_size)
                if ratio > 0.7:
                    return True, "size_outlier_strong"
                if ratio > 0.5:
                    return True, "size_outlier"

    return False, ""



def _normalize_remote_path(path: str) -> str:
    cleaned = [segment for segment in str(path or "").split("/") if segment]
    if not cleaned:
        return ""
    return "/" + "/".join(cleaned)


def _join_remote_path(*parts: str) -> str:
    cleaned = [str(part or "").strip("/") for part in parts if str(part or "").strip("/")]
    if not cleaned:
        return ""
    return "/" + "/".join(cleaned)


def _remote_dirname(path: str) -> str:
    value = _normalize_remote_path(path).rstrip("/")
    if not value or "/" not in value[1:]:
        return ""
    return value.rsplit("/", 1)[0]


def _resolve_parent_path_for_move(
    current_cid: str,
    library_task_key: str,
    source_dir: str,
    target_dir: str,
    source_cid: str,
    target_cid: str,
) -> str:
    cid = str(current_cid or "")
    if not cid:
        return ""

    normalized_source_dir = _normalize_remote_path(source_dir)
    normalized_target_dir = _normalize_remote_path(target_dir)

    if source_cid and cid == str(source_cid):
        return normalized_source_dir
    if target_cid and cid == str(target_cid):
        return normalized_target_dir

    cached_parent = get_task_item_by_id(library_task_key, cid)
    return _normalize_remote_path(str((cached_parent or {}).get("path", "") or ""))


def _is_video_cache_item(item: dict) -> bool:
    if not isinstance(item, dict) or item.get("is_dir"):
        return False
    return os.path.splitext(str(item.get("name", "") or ""))[1].lower() in VIDEO_EXTS


def _target_event_file_class(name: str, is_dir: bool = False) -> str:
    if is_dir:
        return "folder"
    return classify_file(
        str(name or ""),
        VIDEO_EXTS,
        set(DEFAULT_AUDIO_EXTS.split(",")),
        set(DEFAULT_IMAGE_EXTS.split(",")),
        set(DEFAULT_DATA_EXTS.split(",")),
    )


def _build_target_cache_item(item: dict) -> tuple[str, dict] | None:
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
        parent_id = int(item.get("parent_id", item.get("cid", 0)) or 0)
    except (TypeError, ValueError):
        parent_id = 0
    return str(item_id), {
        "name": str(item.get("name", "") or ""),
        "path": str(item.get("path", "") or ""),
        "pickcode": str(item.get("pickcode", item.get("pick_code", "")) or ""),
        "size": size,
        "id": item_id,
        "sha1": str(item.get("sha1", "") or ""),
        "is_dir": bool(item.get("is_dir", False)),
        "parent_id": parent_id,
    }


def _normalize_target_entry(item: dict) -> dict | None:
    cache_item = _build_target_cache_item(item)
    if not cache_item:
        return None
    item_key, cache_data = cache_item
    file_class = _target_event_file_class(cache_data.get("name", ""), cache_data.get("is_dir", False))
    return {
        "item_key": item_key,
        "path": cache_data["path"],
        "name": cache_data["name"],
        "file_id": item_key,
        "pickcode": cache_data.get("pickcode", ""),
        "size": cache_data.get("size", 0),
        "sha1": cache_data.get("sha1", ""),
        "parent_id": cache_data.get("parent_id", 0),
        "is_dir": cache_data.get("is_dir", False),
        "file_class": file_class,
        "cache_item": cache_data,
    }


def _normalize_wash_resolution(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "2160" in text or text == "4k" or "uhd" in text:
        return "2160p"
    if "1080" in text:
        return "1080p"
    if "720" in text:
        return "720p"
    return ""


def _normalize_wash_codec(value: str) -> str:
    text = str(value or "").strip().upper().replace(".", "")
    text = text.replace(" ", "")
    if not text:
        return ""
    if any(token in text for token in ("H265", "X265", "HEVC")):
        return "H265"
    if any(token in text for token in ("H264", "X264", "AVC")):
        return "H264"
    if "AV1" in text:
        return "AV1"
    if "XVID" in text:
        return "XVID"
    if "DIVX" in text:
        return "DIVX"
    if "MPEG4" in text or text == "MPEG4":
        return "MPEG4"
    return ""


def _extract_wash_meta_from_parsed(parsed: dict) -> dict:
    meta_info = dict((parsed or {}).get("meta_info") or {})
    return {
        "resource_pix": _normalize_wash_resolution(meta_info.get("resource_pix")),
        "video_encode": _normalize_wash_codec(meta_info.get("video_encode")),
    }


async def _probe_media_fields_via_ffprobe(file_item: dict, drive_index: int, direct_url: str = "") -> dict:
    pickcode = str((file_item or {}).get("pickcode", "") or "")
    file_name = str((file_item or {}).get("name", "") or "")
    if not pickcode:
        return {}
    resolved_direct_url = str(direct_url or "").strip()
    if not resolved_direct_url:
        resolved_direct_url = await _get_115_direct_url(pickcode, drive_index)
    if not resolved_direct_url:
        return {}
    loop = asyncio.get_event_loop()
    try:
        from core.meta.mediainfo import extract_media_fields

        def _run_probe_with_gate():
            with _FFPROBE_MEDIA_GATE:
                return extract_media_fields(resolved_direct_url)

        fields = await asyncio.wait_for(
            loop.run_in_executor(None, _run_probe_with_gate),
            timeout=_FFPROBE_EXEC_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(f"[MediaOrganize] ffprobe探测超时: file={file_name}, pickcode={pickcode}")
        return {}
    except Exception as e:
        logger.debug(f"[MediaOrganize] ffprobe探测失败: pickcode={pickcode}, err={e}")
        return {}
    normalized = _normalize_ffprobe_fields(fields or {})
    if normalized:
        _set_cached_ffprobe_fields(file_item, normalized)
    return normalized


async def _probe_wash_fields(pickcode: str, drive_index: int, file_name: str = "") -> dict:
    if not pickcode:
        return {}
    direct_url = await _get_115_direct_url(pickcode, drive_index)
    if not direct_url:
        return {}
    loop = asyncio.get_event_loop()
    try:
        def _run_wash_with_gate():
            with _FFPROBE_MEDIA_GATE:
                return extract_wash_fields(direct_url)

        fields = await asyncio.wait_for(
            loop.run_in_executor(None, _run_wash_with_gate),
            timeout=_FFPROBE_EXEC_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(f"[Wash] ffprobe探测超时: file={file_name}, pickcode={pickcode}")
        return {}
    except Exception as e:
        logger.debug(f"[Wash] ffprobe探测失败: pickcode={pickcode}, err={e}")
        return {}
    return {
        "resource_pix": _normalize_wash_resolution(fields.get("resource_pix")),
        "video_encode": _normalize_wash_codec(fields.get("video_encode")),
        "duration_seconds": float(fields.get("duration_seconds")) if fields.get("duration_seconds") else 0.0,
    }


async def _resolve_wash_media_info(file_item: dict, parsed: dict, drive_index: int,
                                   preferred: dict | None = None, allow_probe: bool = True,
                                   require_duration: bool = False) -> dict:
    preferred = preferred or {}
    info = {
        "resource_pix": _normalize_wash_resolution(preferred.get("resource_pix") or (parsed or {}).get("resource_pix") or ""),
        "video_encode": _normalize_wash_codec(preferred.get("video_encode") or (parsed or {}).get("video_encode") or ""),
        "duration_seconds": 0.0,
    }
    should_probe = allow_probe and ((not info["resource_pix"]) or (not info["video_encode"]))
    if should_probe:
        probed = await _probe_wash_fields(
            str((file_item or {}).get("pickcode", "") or ""),
            drive_index,
            file_name=str((file_item or {}).get("name", "") or ""),
        )
        if probed.get("resource_pix"):
            info["resource_pix"] = probed["resource_pix"]
        if probed.get("video_encode"):
            info["video_encode"] = probed["video_encode"]
        if probed.get("duration_seconds"):
            info["duration_seconds"] = probed["duration_seconds"]
    return info



def _select_existing_candidate(candidates: list[dict], expected_name: str = "") -> tuple[Optional[dict], str]:
    if not candidates:
        return None, "no_candidate"
    if expected_name:
        exact = [item for item in candidates if str(item.get("name", "") or "") == expected_name]
        if len(exact) == 1:
            return exact[0], "exact_name"
        if len(exact) > 1:
            return None, "ambiguous_exact_name"
    if len(candidates) == 1:
        return candidates[0], "single_candidate"
    return None, "ambiguous_candidates"


def _count_error_results(results: list[dict]) -> int:
    return sum(1 for item in results if item.get("status") == "error")


def _compare_equivalent_size_wash(new_size: int, new_info: dict, old_size: int, old_info: dict,
                                  tolerance_ratio: float = 0.0) -> dict:
    new_resolution = _normalize_wash_resolution(new_info.get("resource_pix"))
    old_resolution = _normalize_wash_resolution(old_info.get("resource_pix"))
    new_codec = _normalize_wash_codec(new_info.get("video_encode"))
    old_codec = _normalize_wash_codec(old_info.get("video_encode"))

    if not new_resolution or not old_resolution or not new_codec or not old_codec:
        return {"decision": "keep_existing", "reason": "missing_resolution_or_codec"}

    new_multiplier = _WASH_CODEC_MULTIPLIERS.get(new_codec)
    old_multiplier = _WASH_CODEC_MULTIPLIERS.get(old_codec)
    if not new_multiplier or not old_multiplier:
        return {"decision": "keep_existing", "reason": "missing_codec_multiplier"}

    new_equivalent_size = (float(new_size) / (1024 ** 3)) * new_multiplier
    old_equivalent_size = (float(old_size) / (1024 ** 3)) * old_multiplier
    threshold = old_equivalent_size * (1.0 - tolerance_ratio)
    if new_equivalent_size > threshold:
        return {
            "decision": "replace_existing",
            "reason": "equivalent_size_higher",
            "new_equivalent_size": new_equivalent_size,
            "old_equivalent_size": old_equivalent_size,
            "new_gbph": 0.0,
            "old_gbph": 0.0,
        }
    return {
        "decision": "keep_existing",
        "reason": "equivalent_size_not_enough",
        "new_equivalent_size": new_equivalent_size,
        "old_equivalent_size": old_equivalent_size,
        "new_gbph": 0.0,
        "old_gbph": 0.0,
    }


async def _evaluate_library_replacement(config_data: dict, drive_index: int,
                                        media_type: str, season_number: Optional[int],
                                        episode_number: Optional[int], target_base: str,
                                        file_item: dict, parsed: dict, variables: dict, ext: str,
                                        library_index) -> dict:
    if not (config_data.get("wash_enabled") and config_data.get("wash_by_equivalent_size")):
        return {"decision": "disabled"}

    folder_format = config_data.get(
        "movie_folder_format" if media_type == "movie" else "tv_folder_format",
        "{title} ({year}) {tmdb-{tmdb_id}}",
    )
    expected_folder = _render_template(folder_format, variables)
    if not expected_folder:
        return {"decision": "disabled", "reason": "missing_folder_name"}

    if media_type == "movie":
        candidate_dir = _join_remote_path(target_base, expected_folder)
        rename_format = config_data.get("movie_rename_format", "{en_title}.{year}.{resource_pix}.{web_source}.{resource_type}.{resource_effect}.{video_encode}.{color_depth}.{video_effect}.{fps}.{audio_encode}-{resource_team}")
        expected_name = _render_template(rename_format, variables) + ext
    else:
        season_num = season_number if season_number is not None else 1
        candidate_dir = _join_remote_path(target_base, expected_folder, f"Season {season_num:02d}")
        rename_format = config_data.get("tv_episode_format", "{en_title}.{season_episode}.{year}.{resource_pix}.{web_source}.{resource_type}.{video_encode}.{color_depth}.{video_effect}.{fps}.{audio_encode}-{resource_team}")
        expected_name = _render_template(rename_format, variables) + ext

    candidates = library_index.find_existing_candidates(
        media_type,
        candidate_dir,
        season_number,
        episode_number,
    )
    candidate, match_reason = _select_existing_candidate(candidates, expected_name=expected_name)
    if not candidate:
        if match_reason == "no_candidate":
            return {"decision": "no_candidate"}
        return {"decision": "keep_existing", "reason": match_reason}

    expected_parsed = _parse_filename(
        expected_name,
        media_type_hint=media_type,
        file_path=_join_remote_path(candidate_dir, expected_name),
    ) or {}
    existing_parsed = _parse_filename(
        str(candidate.get("name", "") or ""),
        media_type_hint=media_type,
        file_path=str(candidate.get("path", "") or ""),
    ) or {}
    allow_wash_probe = (str(config_data.get("organize_parse_mode") or "filename").lower() == "filename")

    new_info = await _resolve_wash_media_info(
        file_item,
        {},
        drive_index,
        preferred=_extract_wash_meta_from_parsed(expected_parsed),
        allow_probe=allow_wash_probe,
    )
    old_info = await _resolve_wash_media_info(
        candidate,
        _extract_wash_meta_from_parsed(existing_parsed),
        drive_index,
        allow_probe=allow_wash_probe,
    )
    tolerance_percent = float(config_data.get("wash_tolerance_ratio", 0) or 0)
    tolerance_percent = max(0.0, min(tolerance_percent, 99.99))
    comparison = _compare_equivalent_size_wash(
        int(file_item.get("size", 0) or 0),
        new_info,
        int(candidate.get("size", 0) or 0),
        old_info,
        tolerance_ratio=tolerance_percent / 100.0,
    )
    comparison["candidate"] = candidate
    comparison["candidate_dir"] = candidate_dir
    comparison["match_reason"] = match_reason
    comparison["new_info"] = new_info
    comparison["old_info"] = old_info
    return comparison


def _remove_library_candidate(client, candidate: dict, failed_dir_cid: str = "", main_loop: asyncio.AbstractEventLoop | None = None) -> tuple[bool, str]:
    candidate_id = int(candidate.get("id", 0) or 0)
    if not candidate_id:
        return False, "missing_candidate_id"
    try:
        if failed_dir_cid:
            if main_loop is not None:
                _await_on_main_loop(_move_115_items(client, candidate_id, str(failed_dir_cid)), main_loop)
            else:
                _run_115_write_request_sync(
                    client,
                    "移动旧文件到失败目录",
                    lambda write_client: write_client.fs_move_app(candidate_id, pid=int(failed_dir_cid), app="android", async_=False),
                )
            return True, "moved_to_failed"
        _run_115_write_request_sync(
            client,
            "删除旧文件",
            lambda write_client: write_client.fs_delete([candidate_id], async_=False),
        )
        return True, "deleted"
    except Exception as e:
        logger.warning(f"[Wash] 处理旧文件失败: id={candidate_id}, err={e}")
        return False, str(e)


async def _dedupe_pending_tv_plan_item(config_data: dict, drive_index: int, pending_tv_batches: dict,
                                       batch_key, incoming_plan: dict) -> tuple[bool, Optional[dict], str]:
    if not (config_data.get("wash_enabled") and config_data.get("wash_by_equivalent_size")):
        return True, None, "wash_disabled"

    batch_entry = pending_tv_batches.get(batch_key) or {}
    season_num = incoming_plan.get("season_num")
    episode_num = incoming_plan.get("episode_num")
    if season_num is None or episode_num is None:
        return True, None, "missing_season_or_episode"

    episode_key = (season_num, episode_num)
    item_index = batch_entry.get("item_index") or {}
    existing_plan = item_index.get(episode_key)
    if not existing_plan:
        return True, None, "no_duplicate"

    allow_wash_probe = (str(config_data.get("organize_parse_mode") or "filename").lower() == "filename")
    incoming_expected = str(incoming_plan.get("renamed_file", "") or "")
    existing_expected = str(existing_plan.get("renamed_file", "") or "")
    incoming_expected_parsed = _parse_filename(
        incoming_expected,
        media_type_hint="tv",
        file_path=_join_remote_path(str(incoming_plan.get("season_dir_path", "") or ""), incoming_expected),
    ) or {}
    existing_expected_parsed = _parse_filename(
        existing_expected,
        media_type_hint="tv",
        file_path=_join_remote_path(str(existing_plan.get("season_dir_path", "") or ""), existing_expected),
    ) or {}

    new_info = await _resolve_wash_media_info(
        incoming_plan.get("vf") or {},
        {},
        drive_index,
        preferred=_extract_wash_meta_from_parsed(incoming_expected_parsed),
        allow_probe=allow_wash_probe,
    )
    old_info = await _resolve_wash_media_info(
        existing_plan.get("vf") or {},
        {},
        drive_index,
        preferred=_extract_wash_meta_from_parsed(existing_expected_parsed),
        allow_probe=allow_wash_probe,
    )
    tolerance_percent = float(config_data.get("wash_tolerance_ratio", 0) or 0)
    tolerance_percent = max(0.0, min(tolerance_percent, 99.99))
    comparison = _compare_equivalent_size_wash(
        int((incoming_plan.get("vf") or {}).get("size", 0) or 0),
        new_info,
        int((existing_plan.get("vf") or {}).get("size", 0) or 0),
        old_info,
        tolerance_ratio=tolerance_percent / 100.0,
    )
    comparison_reason = str(comparison.get("reason", "duplicate_episode_keep_existing"))
    comparison["new_info"] = new_info
    comparison["old_info"] = old_info

    if comparison.get("decision") == "replace_existing":
        return True, existing_plan, comparison_reason
    return False, existing_plan, comparison_reason


def _build_source_poll_session_key(drive_index: int, source_cid: str) -> str:
    return f"{int(drive_index)}:{str(source_cid or '')}"


def _update_streaming_progress(run_id: str, *, scanned_video_count: int, processed_result_count: int,
                               success_count: int, error_count: int, strm_generated_count: int,
                               scan_complete: bool = False, status: Optional[str] = None):
    if scan_complete:
        pct = min(int(processed_result_count / scanned_video_count * 90) + 10, 100) if scanned_video_count > 0 else 100
        message = f"整理: {processed_result_count}/{scanned_video_count}"
    else:
        pct = 5 if scanned_video_count <= 0 else min(5 + scanned_video_count // 20, 90)
        message = f"整理: 已扫描 {scanned_video_count} 个视频，已完成 {processed_result_count} 个"
    effective_status = status or "running"
    update_task_progress(run_id, message, pct, effective_status)
    ACTIVE_TASKS[run_id]["detail"] = {
        "total": scanned_video_count,
        "success": success_count,
        "failed": error_count,
        "strm": strm_generated_count,
    }


def _is_organize_cancel_requested(run_id: str) -> bool:
    return bool(ACTIVE_TASKS.get(run_id, {}).get("cancel_requested"))


def _raise_if_organize_cancelled(run_id: str):
    if _is_organize_cancel_requested(run_id):
        logger.info(f"[MediaOrganize] 用户取消整理: run_id={run_id}")
        raise _OrganizeCancelledError("用户取消")


def _await_on_main_loop(coro, main_loop: asyncio.AbstractEventLoop):
    if main_loop is None:
        raise RuntimeError("主事件循环未注册")
    if main_loop.is_closed():
        raise RuntimeError("主事件循环已关闭")
    if not main_loop.is_running():
        raise RuntimeError("主事件循环未运行")
    return asyncio.run_coroutine_threadsafe(coro, main_loop).result()


async def _reconcile_late_subtitles(client, subtitles_by_parent: dict, organized_targets_by_parent: dict):
    compensated = 0
    for parent_id, targets in list((organized_targets_by_parent or {}).items()):
        if not subtitles_by_parent.get(parent_id):
            continue
        for target in targets:
            file_item = target.get("file_item") or {}
            renamed_file = str(target.get("renamed_file", "") or file_item.get("name", "") or "")
            target_cid = str(target.get("target_cid", "") or "")
            target_path = str(target.get("target_path", "") or "")
            if not renamed_file or not target_cid or not target_path:
                continue
            moved = await _match_and_move_subtitles(
                client,
                file_item,
                renamed_file,
                subtitles_by_parent,
                target_cid=target_cid,
                target_path=target_path,
            )
            compensated += len(moved or [])
    return compensated


async def _scan_source_poll_snapshot(drive_index: int, source_cid: str) -> dict:
    loop = asyncio.get_event_loop()
    client = _get_115_client(drive_index)
    tree_entries = await loop.run_in_executor(
        None,
        _list_115_tree_entries,
        client,
        str(source_cid),
    )
    return {
        "tree_entries": tree_entries,
        "entry_count": len(tree_entries or []),
    }


def _source_scan_signature(snapshot: dict) -> str:
    hasher = hashlib.sha1()
    for item in sorted(snapshot.get("tree_entries", []) or [], key=lambda value: (
        str(value.get("path", "") or ""),
        str(value.get("id") or value.get("fid") or ""),
        str(value.get("name", "") or ""),
    )):
        item_id = str(item.get("id") or item.get("fid") or "")
        parent_id = str(item.get("parent_id", item.get("cid", "")) or "")
        name = str(item.get("name", "") or "")
        path = str(item.get("path", "") or "")
        size = int(item.get("size", 0) or 0)
        sha1 = str(item.get("sha1", "") or "").upper()
        pickcode = str(item.get("pickcode", item.get("pick_code", "")) or "")
        is_dir = "1" if item.get("is_dir") else "0"
        hasher.update(f"{is_dir}:{item_id}:{parent_id}:{name}:{path}:{size}:{sha1}:{pickcode}\n".encode("utf-8", errors="ignore"))
    return hasher.hexdigest()


async def _trigger_auto_organize_and_wait(drive_index: int, source_tree_entries: Optional[list[dict]] = None) -> tuple[str, str]:
    from app.routers.media_organize import organize_media, OrganizeRequest

    while True:
        with _organize_trigger_lock:
            if _state._organize_running:
                done_event = _state._organize_done_event
                wait_for_existing = True
            else:
                _state._organize_running = True
                _state._organize_done_event = asyncio.Event()
                wait_for_existing = False

        if wait_for_existing:
            logger.info("[115Life] 整理任务已在运行，等待现有任务完成后补跑")
            if done_event:
                await done_event.wait()
            else:
                await asyncio.sleep(1)
            source_tree_entries = None
            continue

        try:
            organize_req = OrganizeRequest(drive_index=drive_index)
            if source_tree_entries is not None:
                organize_req._prefetched_source_tree_entries = list(source_tree_entries or [])
            result = await organize_media(organize_req)
            if result.get("status") != "ok":
                return "", str(result.get("message", "启动整理失败") or "启动整理失败")

            run_id = str(result.get("run_id", "") or "")
            if not run_id:
                return "", "缺少 run_id"

            while True:
                task = ACTIVE_TASKS.get(run_id, {})
                status = str(task.get("status", "") or "")
                if status in ("finished", "error", "stopped"):
                    return run_id, status
                await asyncio.sleep(1)
        finally:
            with _organize_trigger_lock:
                _state._organize_running = False
                if _state._organize_done_event:
                    _state._organize_done_event.set()
                    _state._organize_done_event = None


def _schedule_or_refresh_source_poll(drive_index: int, source_dir: str, target_dir: str, source_cid: str, *, phase: str = "pre_run"):
    session_key = _build_source_poll_session_key(drive_index, source_cid)
    now = _time.time()
    desired_phase = "post_run" if str(phase or "") == "post_run" else "pre_run"
    with _source_poll_lock:
        session = _state._source_poll_sessions.get(session_key)
        if session is None:
            session = {
                "session_key": session_key,
                "drive_index": int(drive_index),
                "source_cid": str(source_cid or ""),
                "source_dir": str(source_dir or ""),
                "target_dir": str(target_dir or ""),
                "phase": desired_phase,
                "last_scan_signature": "",
                "unchanged_polls": 0,
                "organize_runs": 0,
                "event_generation": 1,
                "active_run_id": "",
                "started_at": now,
                "max_runs": 20,
            }
            _state._source_poll_sessions[session_key] = session
            logger.info(f"[115Life] 已创建源目录轮询会话: {session_key} source={source_dir} phase={desired_phase}")
        else:
            session["source_dir"] = str(source_dir or session.get("source_dir", ""))
            session["target_dir"] = str(target_dir or session.get("target_dir", ""))
            session["event_generation"] = int(session.get("event_generation", 0) or 0) + 1
            if desired_phase == "post_run":
                session["phase"] = "post_run"
                session["last_scan_signature"] = ""
                session["unchanged_polls"] = 0
            elif session.get("phase") == "pre_run":
                session["unchanged_polls"] = 0
                session["last_scan_signature"] = ""
            logger.info(f"[115Life] 已刷新源目录轮询会话: {session_key} generation={session['event_generation']} phase={session.get('phase')}")

        should_schedule = not _state._source_poll_running
        if should_schedule:
            _state._source_poll_running = True

    if should_schedule:
        if _state._main_event_loop and _state._main_event_loop.is_running():
            asyncio.run_coroutine_threadsafe(_run_source_poll_loop(), _state._main_event_loop)
        else:
            with _source_poll_lock:
                _state._source_poll_running = False
            logger.error("[115Life] 主事件循环未注册，无法启动源目录轮询")


async def _run_source_poll_loop():
    try:
        while True:
            with _source_poll_lock:
                sessions = [dict(s) for s in _state._source_poll_sessions.values()]
            if not sessions:
                with _source_poll_lock:
                    _state._source_poll_running = False
                return

            for session in sessions:
                session_key = str(session.get("session_key", "") or "")
                if not session_key:
                    continue
                current = _state._source_poll_sessions.get(session_key)
                if not current:
                    continue

                drive_index = int(current.get("drive_index", 0) or 0)
                source_cid = str(current.get("source_cid", "") or "")
                if not source_cid:
                    with _source_poll_lock:
                        _state._source_poll_sessions.pop(session_key, None)
                    continue

                snapshot = await _scan_source_poll_snapshot(drive_index, source_cid)
                signature = _source_scan_signature(snapshot)
                entry_count = int(snapshot.get("entry_count", 0) or 0)
                source_tree_entries = list(snapshot.get("tree_entries", []) or [])
                phase = str(current.get("phase", "pre_run") or "pre_run")
                last_signature = str(current.get("last_scan_signature", "") or "")
                unchanged_polls = int(current.get("unchanged_polls", 0) or 0)

                if phase == "pre_run":
                    if last_signature and signature == last_signature:
                        unchanged_polls += 1
                    else:
                        unchanged_polls = 0
                    current["last_scan_signature"] = signature
                    current["unchanged_polls"] = unchanged_polls
                    logger.info(
                        f"[115Life] 源目录轮询: phase=pre_run key={session_key} 条目={entry_count} 签名={signature[:8]} 不变={unchanged_polls}"
                    )
                    if unchanged_polls >= 1:
                        logger.info(f"[115Life] 源目录轮询命中稳定窗口，开始自动整理: key={session_key} reason=pre_run_tree_stable")
                        run_id, run_status = await _trigger_auto_organize_and_wait(drive_index, source_tree_entries)
                        current["active_run_id"] = run_id
                        current["organize_runs"] = int(current.get("organize_runs", 0) or 0) + 1
                        current["phase"] = "post_run"
                        current["last_scan_signature"] = ""
                        current["unchanged_polls"] = 0
                        logger.debug(f"[115Life] 自动整理完成，等待整理后复查 | 会话={session_key} | run_id={run_id} | 状态={run_status}")
                else:
                    logger.debug(
                        f"[115Life] 源目录复查: 整理完成后检查 | 会话={session_key} | 剩余条目={entry_count} | 签名={signature[:8]}"
                    )
                    if entry_count <= 0:
                        with _source_poll_lock:
                            _state._source_poll_sessions.pop(session_key, None)
                        logger.debug(f"[115Life] 源目录已清空，停止自动补跑 | 会话={session_key}")
                        continue
                    if int(current.get("organize_runs", 0) or 0) >= int(current.get("max_runs", 20) or 20):
                        with _source_poll_lock:
                            _state._source_poll_sessions.pop(session_key, None)
                        logger.warning(f"[115Life] 源目录自动补跑达到上限，停止: key={session_key} reason=max_runs_exceeded 条目={entry_count}")
                        continue
                    logger.info(f"[115Life] 源目录仍有残留条目，继续自动补跑: key={session_key} reason=source_still_has_entries count={entry_count}")
                    run_id, run_status = await _trigger_auto_organize_and_wait(drive_index, source_tree_entries)
                    current["active_run_id"] = run_id
                    current["organize_runs"] = int(current.get("organize_runs", 0) or 0) + 1
                    logger.info(f"[115Life] 自动补跑完成: key={session_key} run_id={run_id} status={run_status}")

            await asyncio.sleep(5)
    except Exception as e:
        with _source_poll_lock:
            _state._source_poll_running = False
        logger.error(f"[115Life] 源目录轮询异常退出: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# _run_organize_async  (the master orchestrator)
# ---------------------------------------------------------------------------

async def _run_organize_async(run_id: str, req):
    """
    整理核心逻辑（在后台线程的独立事件循环中运行）。
    扫描源目录 → 自动识别 TMDb 信息 → 重命名 → 移动到目标目录 → 按策略生成元数据。
    每个文件独立识别，支持混合内容（电影+剧集）。
    """
    total_files = 0
    scanned_video_count = 0
    scan_complete = False
    results = []
    skipped_results = []
    success_count = 0
    strm_generated_count = 0
    organized_targets_by_parent: dict[str, list[dict]] = {}
    metadata_executor = None
    pending_library_cache_items: dict[str, dict] = {}
    pending_strm_payloads: list[dict] = []
    pending_emby_library_checks: list[dict] = []
    pending_refresh_payloads: list[dict] = []
    pending_duplicate_moves: list[dict] = []
    pending_wash_reject_moves: list[dict] = []
    duplicate_batch_size = 200

    def _flush_pending_library_cache_updates():
        nonlocal pending_library_cache_items
        if not pending_library_cache_items:
            return 0
        merge_task_items(
            library_task_key,
            pending_library_cache_items,
            meta={"last_status": "updated_by_media_organize"},
        )
        flushed_count = len(pending_library_cache_items)
        logger.info(f"[媒体库缓存] 已批量合并写入整理结果 {flushed_count} 条 | 总条目 {library_index.item_count()}")
        pending_library_cache_items = {}
        return flushed_count

    async def _flush_pending_duplicate_moves():
        nonlocal pending_duplicate_moves
        if not pending_duplicate_moves:
            return 0
        batch = pending_duplicate_moves
        pending_duplicate_moves = []
        logger.info(f"[MediaOrganize] 开始批量移动重复文件: {len(batch)} 条")
        loop = asyncio.get_event_loop()
        batch_results = await loop.run_in_executor(
            None,
            lambda _c=client, _items=batch, _sp=subtitles_by_parent: _execute_duplicate_batch_plan(
                _c,
                _items,
                subtitles_by_parent=_sp,
                main_loop=_state._main_event_loop,
            ),
        )
        success_count_local = sum(1 for item in batch_results if item.get("status") == "success")
        failed_items = [item for item in batch_results if item.get("status") != "success"]
        for item in batch_results:
            file_name = str(item.get("file", "") or "")
            if item.get("status") == "success":
                logger.info(f"[MediaOrganize] 重复文件已移入重复目录: {file_name}")
            else:
                logger.warning(f"[MediaOrganize] 移动重复文件失败: {item.get('message', file_name)}")
        logger.info(f"[MediaOrganize] 重复文件批量移动完成: 成功 {success_count_local}/{len(batch)}")
        return len(failed_items)

    async def _flush_pending_wash_reject_moves():
        nonlocal pending_wash_reject_moves
        if not pending_wash_reject_moves:
            return 0
        batch = pending_wash_reject_moves
        pending_wash_reject_moves = []
        logger.info(f"[Wash] 开始批量移动洗版未通过文件: {len(batch)} 条")
        await _move_failed_files_batch(
            client,
            batch,
            str(source_cid),
            wash_dir_cid,
            moved_dirs,
            subtitles_by_parent=subtitles_by_parent,
        )
        moved_count = sum(1 for item in batch if str(item.get("id") or item.get("fid", "")) in moved_dirs)
        logger.info(f"[Wash] 洗版未通过文件批量移动完成: {moved_count}/{len(batch)}")
        return len(batch) - moved_count

    def _flush_pending_media_server_refreshes(immediate: bool = False):
        nonlocal pending_refresh_payloads
        if not pending_refresh_payloads:
            return 0
        payloads = pending_refresh_payloads
        pending_refresh_payloads = []
        log_prefix = "分组完成后立即触发媒体库刷新" if immediate else "元数据任务完成后触发媒体库刷新"
        logger.info(f"[MediaOrganize] {log_prefix}: {len(payloads)} 个路径")
        if immediate:
            threading.Thread(
                target=media_server_refresh.refresh_immediately,
                args=(payloads,),
                daemon=True,
            ).start()
            return len(payloads)
        for refresh_payload in payloads:
            threading.Thread(
                target=media_server_refresh.refresh,
                args=(refresh_payload,),
                daemon=True,
            ).start()
        return len(payloads)

    try:
        config_data = await _load_config_data()
        drive_index = req.drive_index or config_data.get("drive_index", 0)
        target_cid = config_data.get("target_cid", "0")
        source_cid = config_data.get("source_cid", "0")

        logger.info("[MediaOrganize] 执行整理任务")
        logger.info(f"[MediaOrganize] source_cid={source_cid}, target_cid={target_cid}, drive_index={drive_index}")

        # 1. 获取 115 客户端
        client = _get_115_client(drive_index)

        loop = asyncio.get_event_loop()
        subtitles_by_parent: dict[str, list[dict]] = {}

        # 2. 获取刮削配置
        scraping_config = _build_scraping_config(config_data)
        _org_start = _time.time()
        metadata_executor = ThreadPoolExecutor(max_workers=20)

        # 3. 缓存已识别的 TMDb 数据（同名文件不用重复查）
        tmdb_cache = {}  # key: (tmdb_id, media_type) → tmdb_data
        _tmdb_fetch_locks: dict[int, asyncio.Lock] = {}  # 防止并发组对同一部剧重复聚合

        # 4. 本次任务的搜索失败缓存
        failed_cache = set()
        search_cache = {}

        # 失败目录配置（提前到扫描阶段，以便 SHA1 去重时也能移动）
        failed_cid = config_data.get("failed_cid", "0")
        has_failed_dir = failed_cid and failed_cid != "0" and failed_cid != 0
        failed_dir_cid = str(failed_cid) if has_failed_dir else ""
        dedup_cid = config_data.get("dedup_cid", "0")
        dedup_dir_cid = str(dedup_cid) if dedup_cid and dedup_cid != "0" and dedup_cid != 0 else failed_dir_cid
        wash_cid = config_data.get("wash_cid", "0")
        wash_dir_cid = str(wash_cid) if wash_cid and wash_cid != "0" and wash_cid != 0 else failed_dir_cid
        moved_dirs: set = set()
        fs = _get_115_fs(client)
        target_remote_path = str(config_data.get("target_name", "") or "")
        library_task_key = build_task_key(drive_index, target_remote_path)

        library_index = get_task_index(library_task_key)

        # 初始化二级分类匹配器与目录链缓存（任务作用域）
        from app.services.category_matcher import CategoryMatcher
        _category_matcher = CategoryMatcher()
        _dir_chain_cache: dict = {}
        _category_match_cache: dict = {}
        _tv_summary_logged: set[str] = set()
        target_sha1_count = library_index.sha1_count()
        if target_sha1_count:
            logger.debug(f"[MediaOrganize] 已加载媒体库缓存SHA1索引: {target_sha1_count} 个 | 条目={library_index.item_count()} | task={library_task_key}")
        else:
            logger.warning(f"[MediaOrganize] 未命中媒体库缓存，当前整理将跳过缓存排重: task={library_task_key}")

        from core.configs import global_config
        api_key = global_config.tmdb_key
        if not api_key:
            update_task_progress(run_id, "整理失败: TMDb API Key 未配置", 100, "error")
            return

        async def _search_group(key, group):
            """按分组并发搜索（不额外限制并发）"""
            first_vf, first_parsed, _ = group[0]
            logger.info(f"[MediaOrganize] 开始识别: {first_vf.get('name', '')}")

            if first_parsed["tmdb_id_direct"]:
                direct_result = {
                    "tmdb_id": first_parsed["tmdb_id_direct"],
                    "media_type": first_parsed["media_type"],
                    "title": first_parsed["title"],
                }
                return key, direct_result

            result = await _search_tmdb_for_title(first_parsed, api_key, failed_cache)
            if not result:
                logger.info(f"[MediaOrganize] TMDb失败: key={key} 标题={first_parsed.get('title','')}")
            return key, result

        logger.debug("[MediaOrganize] 阶段1/4: 流式扫描源目录并持续提交分组")
        _update_streaming_progress(
            run_id,
            scanned_video_count=0,
            processed_result_count=0,
            success_count=0,
            error_count=0,
            strm_generated_count=0,
            scan_complete=False,
        )

        # === Phase 2+3 流水线：扫到闭合分组后立即整理，不等全量扫描结束 ===
        async def _search_and_organize(key, group):
            nonlocal success_count, strm_generated_count
            group_started_at = _time.time()
            loop = asyncio.get_event_loop()
            try:
                sr = await _search_group(key, group)
                _, result = sr
                search_cache[key] = result
            except _OrganizeCancelledError:
                raise
            except asyncio.CancelledError:
                raise _OrganizeCancelledError("用户取消")
            except Exception as e:
                logger.error(f"[MediaOrganize] 搜索异常: {e}")
                search_cache[key] = None

            group_failed = []
            tv_root_scraped = set()
            season_scraped = set()
            pending_tv_batches = {}
            ffprobe_batch_profiles: dict = {}
            ffprobe_batch_sizes: dict = {}
            ffprobe_batch_segment_samples: dict = {}
            ffprobe_stats = {"sample": 0, "anomaly": 0, "cache": 0, "reuse": 0, "batch_cache": 0, "mismatch": 0, "full_probe": 0, "segment": 0}

            def _record_ffprobe_segment(ffprobe_batch_key: tuple, sampled_profile: dict, current_size: int, file_name: str, reason: str, from_cache: bool = False) -> None:
                segment_state = ffprobe_batch_segment_samples.get(ffprobe_batch_key)
                segment_state, segment_reset = _record_ffprobe_segment_sample(segment_state, sampled_profile, current_size)
                ffprobe_batch_segment_samples[ffprobe_batch_key] = segment_state
                if segment_reset:
                    ffprobe_stats["mismatch"] += 1
                    logger.debug(f"[MediaOrganize] FFPROBE分段样本重置: {file_name} | batch={ffprobe_batch_key}")
                segment_profile = segment_state.get("profile") or {}
                segment_sample_count = int(segment_profile.get("sample_count", 0) or 0)
                if segment_sample_count >= _FFPROBE_BATCH_SAMPLE_LIMIT:
                    ffprobe_batch_profiles[ffprobe_batch_key] = segment_profile
                    ffprobe_batch_sizes[ffprobe_batch_key] = list(segment_state.get("sizes") or [])
                    _set_cached_ffprobe_batch_profile(ffprobe_batch_key, segment_profile)
                    ffprobe_batch_segment_samples.pop(ffprobe_batch_key, None)
                    ffprobe_stats["segment"] += 1
                    logger.debug(f"[MediaOrganize] FFPROBE分段样本切换: {file_name} | batch={ffprobe_batch_key} | 样本数={segment_sample_count}")
                else:
                    source_label = "缓存" if from_cache else "探测"
                    logger.debug(f"[MediaOrganize] FFPROBE分段采样{source_label}: {file_name} | batch={ffprobe_batch_key} | 样本数={segment_sample_count}/{_FFPROBE_BATCH_SAMPLE_LIMIT} | 原因={reason}")

            ffprobe_group_urls: dict[str, str] = {}
            group_parse_mode = (config_data.get("organize_parse_mode") or "filename").lower()
            if group_parse_mode == "ffprobe_full":
                group_pickcodes = [
                    str((vf or {}).get("pickcode", "") or "").strip()
                    for vf, _, _ in group
                    if str((vf or {}).get("pickcode", "") or "").strip()
                ]
                ffprobe_group_urls = await _get_115_direct_urls(group_pickcodes, drive_index)
                logger.debug(f"[MediaOrganize] FFPROBE全量批量取链: 组内={len(group_pickcodes)} 命中={len(ffprobe_group_urls)}")
            elif group_parse_mode == "ffprobe":
                logger.debug("[MediaOrganize] FFPROBE智能懒加载取链: 按需获取")

            _title_cache = {}
            _recognition_summary_logged: set[tuple] = set()
            for group_index, (vf, parsed, ext) in enumerate(group, 1):
                file_name = vf["name"]
                file_item = vf

                try:
                    search_result = search_cache.get(key)

                    if not search_result:
                        results.append({"file": file_name, "status": "error", "message": "无法识别媒体信息"})
                        group_failed.append(file_item)
                        continue

                    tmdb_id = search_result["tmdb_id"]
                    media_type = parsed["media_type"]
                    season_num = search_result.get("season", parsed["season"])
                    episode_num = search_result.get("episode", parsed["episode"])

                    # 获取 TMDb 数据（带缓存 + 并发锁，防止同一部剧被多个组重复聚合）
                    cache_key = (tmdb_id, media_type)
                    if cache_key not in tmdb_cache:
                        if tmdb_id not in _tmdb_fetch_locks:
                            _tmdb_fetch_locks[tmdb_id] = asyncio.Lock()
                        async with _tmdb_fetch_locks[tmdb_id]:
                            if cache_key not in tmdb_cache:
                                tmdb_data = await _fetch_tmdb_data(tmdb_id, media_type, season_num, parsed)
                                tmdb_cache[cache_key] = tmdb_data
                            else:
                                tmdb_data = tmdb_cache[cache_key]
                    else:
                        tmdb_data = tmdb_cache[cache_key]
                    if not tmdb_data:
                        results.append({"file": file_name, "status": "error",
                                       "message": f"无法获取 TMDb 数据 (ID: {tmdb_id})"})
                        group_failed.append(file_item)
                        continue

                    source = tmdb_data.get("series_details") if "series_details" in tmdb_data else tmdb_data
                    if media_type == 'movie':
                        recognized_title = source.get("title") or source.get("original_title") or file_name
                        recognized_year = (source.get("release_date") or "0000")[:4]
                    else:
                        recognized_title = source.get("name") or source.get("original_name") or file_name
                        recognized_year = (source.get("first_air_date") or "0000")[:4]

                    if media_type == 'tv':
                        recognition_summary_key = (str(tmdb_id), media_type, season_num)
                        if recognition_summary_key not in _recognition_summary_logged:
                            _recognition_summary_logged.add(recognition_summary_key)
                            logger.info(
                                f"[MediaOrganize] 识别结果: {recognized_title} ({recognized_year}) TMDb:{tmdb_id} 类型:{media_type} 季:{season_num} 本组:{len(group)} 集"
                            )
                        logger.debug(
                            f"[MediaOrganize] 识别结果: {file_name} -> {recognized_title} ({recognized_year}) TMDb:{tmdb_id} 类型:{media_type} 季:{season_num} 集:{episode_num}"
                        )
                    else:
                        logger.info(
                            f"[MediaOrganize] 识别结果: {file_name} -> {recognized_title} ({recognized_year}) TMDb:{tmdb_id} 类型:{media_type} 季:{season_num} 集:{episode_num}"
                        )

                    if media_type == 'tv' and episode_num is None:
                        logger.warning(f"[MediaOrganize] 剧集缺少集号，跳过整理: {file_name}")
                        results.append({"file": file_name, "status": "error", "message": "剧集缺少集号，无法安全整理"})
                        group_failed.append(file_item)
                        _processed = len(results)
                        _update_streaming_progress(
                            run_id,
                            scanned_video_count=scanned_video_count,
                            processed_result_count=_processed,
                            success_count=success_count,
                            error_count=_count_error_results(results),
                            strm_generated_count=strm_generated_count,
                            scan_complete=scan_complete,
                        )
                        _raise_if_organize_cancelled(run_id)
                        continue
                    if media_type == 'tv' and season_num is None:
                        season_num = 1

                    # 构建模板变量
                    file_req = type('Obj', (), {
                        'media_type': media_type,
                        'tmdb_id': tmdb_id,
                        'season_number': season_num,
                        'episode_number': episode_num,
                        'is_bluray': req.is_bluray,
                        'drive_index': req.drive_index,
                        'overwrite': req.overwrite,
                    })()
                    meta_info = parsed.get("meta_info", {})
                    parse_mode = (config_data.get("organize_parse_mode") or "filename").lower()
                    use_smart_ffprobe_mode = parse_mode == "ffprobe"
                    use_full_ffprobe_mode = parse_mode == "ffprobe_full"
                    variables = _build_template_variables(tmdb_data, file_req, ext, meta_info, _title_cache=_title_cache)
                    if use_full_ffprobe_mode:
                        pickcode = str((file_item or {}).get("pickcode", "") or "").strip()
                        direct_url = ffprobe_group_urls.get(pickcode, "") if pickcode else ""
                        probe_fields = await _probe_media_fields_via_ffprobe(file_item, drive_index, direct_url=direct_url)
                        if probe_fields:
                            variables = _merge_probe_fields_into_variables(variables, probe_fields)
                        ffprobe_stats["full_probe"] += 1
                    elif use_smart_ffprobe_mode:
                        ffprobe_batch_key = _build_ffprobe_batch_key(tmdb_id, parsed, file_item, ext)
                        current_size = int(file_item.get("size", 0) or 0)
                        batch_sizes = ffprobe_batch_sizes.setdefault(ffprobe_batch_key, [])
                        batch_profile = ffprobe_batch_profiles.get(ffprobe_batch_key)
                        if batch_profile is None:
                            disk_batch_profile = _get_cached_ffprobe_batch_profile(ffprobe_batch_key)
                            if disk_batch_profile:
                                ffprobe_batch_profiles[ffprobe_batch_key] = disk_batch_profile
                                batch_profile = disk_batch_profile
                                ffprobe_stats["batch_cache"] += 1
                        is_segment_sampling = ffprobe_batch_key in ffprobe_batch_segment_samples
                        cached_probe = _get_cached_ffprobe_fields(file_item)
                        if cached_probe:
                            variables = _merge_probe_fields_into_variables(variables, cached_probe)
                            ffprobe_stats["cache"] += 1
                            sampled_profile = _make_ffprobe_sample_profile(cached_probe, ext)
                            if is_segment_sampling:
                                _record_ffprobe_segment(ffprobe_batch_key, sampled_profile, current_size, file_name, "segment_sampling", from_cache=True)
                            elif batch_profile:
                                is_anomaly, anomaly_reason = _is_ffprobe_anomaly(file_item, parsed, batch_profile, batch_sizes)
                                if _is_ffprobe_size_anomaly_reason(anomaly_reason):
                                    ffprobe_stats["anomaly"] += 1
                                    _record_ffprobe_segment(ffprobe_batch_key, sampled_profile, current_size, file_name, anomaly_reason, from_cache=True)
                                elif not is_anomaly:
                                    _append_ffprobe_batch_size(batch_sizes, current_size)
                        else:
                            is_anomaly, anomaly_reason = _is_ffprobe_anomaly(file_item, parsed, batch_profile, batch_sizes)
                            should_probe = is_segment_sampling or not batch_profile or is_anomaly
                            if batch_profile and not should_probe:
                                variables = _merge_probe_fields_into_variables(variables, batch_profile)
                                _append_ffprobe_batch_size(batch_sizes, current_size)
                                ffprobe_stats["reuse"] += 1
                            else:
                                pickcode = str((file_item or {}).get("pickcode", "") or "").strip()
                                direct_url = ffprobe_group_urls.get(pickcode, "") if pickcode else ""
                                probe_fields = await _probe_media_fields_via_ffprobe(file_item, drive_index, direct_url=direct_url)
                                if probe_fields:
                                    variables = _merge_probe_fields_into_variables(variables, probe_fields)
                                    sampled_profile = _make_ffprobe_sample_profile(probe_fields, ext)
                                    if is_segment_sampling:
                                        ffprobe_stats["sample"] += 1
                                        _record_ffprobe_segment(ffprobe_batch_key, sampled_profile, current_size, file_name, "segment_sampling")
                                    elif batch_profile is None:
                                        ffprobe_batch_profiles[ffprobe_batch_key] = sampled_profile
                                        _append_ffprobe_batch_size(batch_sizes, current_size)
                                        _set_cached_ffprobe_batch_profile(ffprobe_batch_key, sampled_profile)
                                        ffprobe_stats["sample"] += 1
                                        logger.debug(f"[MediaOrganize] FFPROBE样本探测: {file_name} | batch={ffprobe_batch_key}")
                                    else:
                                        sample_count = int((batch_profile or {}).get("sample_count", 0) or 0)
                                        if anomaly_reason == "need_more_samples" and sample_count < _FFPROBE_BATCH_SAMPLE_LIMIT:
                                            if _profiles_match_for_batch(batch_profile, sampled_profile):
                                                merged_profile = _merge_ffprobe_profiles(batch_profile, {
                                                    **sampled_profile,
                                                    "sample_count": sample_count + 1,
                                                })
                                                ffprobe_batch_profiles[ffprobe_batch_key] = merged_profile
                                                _append_ffprobe_batch_size(batch_sizes, current_size)
                                                _set_cached_ffprobe_batch_profile(ffprobe_batch_key, merged_profile)
                                                ffprobe_stats["sample"] += 1
                                                logger.debug(f"[MediaOrganize] FFPROBE补充样本: {file_name} | batch={ffprobe_batch_key} | 样本数={sample_count + 1}")
                                            else:
                                                ffprobe_stats["mismatch"] += 1
                                                ffprobe_stats["anomaly"] += 1
                                                logger.debug(f"[MediaOrganize] FFPROBE样本不一致: {file_name} | batch={ffprobe_batch_key}")
                                        elif _is_ffprobe_size_anomaly_reason(anomaly_reason):
                                            ffprobe_stats["anomaly"] += 1
                                            _record_ffprobe_segment(ffprobe_batch_key, sampled_profile, current_size, file_name, anomaly_reason)
                                        else:
                                            ffprobe_stats["anomaly"] += 1
                                            logger.debug(f"[MediaOrganize] FFPROBE异常探测: {file_name} | 原因={anomaly_reason}")
                                elif batch_profile and not is_anomaly and not is_segment_sampling:
                                    variables = _merge_probe_fields_into_variables(variables, batch_profile)
                                    _append_ffprobe_batch_size(batch_sizes, current_size)
                                    ffprobe_stats["reuse"] += 1

                    # 二级分类：计算有效目标目录
                    category_cache_key = (str(tmdb_id), media_type)
                    if category_cache_key in _category_match_cache:
                        category_path = _category_match_cache[category_cache_key]
                    else:
                        category_path = _category_matcher.match(tmdb_data, media_type)
                        _category_match_cache[category_cache_key] = category_path
                    target_name = str(config_data.get("target_name", "") or "").strip()
                    target_base = target_name.rstrip("/") if target_name else ""
                    if category_path and category_path != "其他":
                        effective_target_cid = _ensure_115_dir_chain_cached(
                            client,
                            str(target_cid),
                            category_path,
                            _dir_chain_cache,
                            task_key=library_task_key,
                            base_path=target_base,
                        )
                        logger.debug(f"[CategoryDir] {file_name} -> {category_path} (cid={effective_target_cid})")
                    else:
                        effective_target_cid = str(target_cid)

                    if category_path and category_path != "其他":
                        target_base = f"{target_base}/{category_path}" if target_base else category_path

                    wash_result = await _evaluate_library_replacement(
                        config_data=config_data,
                        drive_index=drive_index,
                        media_type=media_type,
                        season_number=season_num,
                        episode_number=episode_num,
                        target_base=target_base,
                        file_item=file_item,
                        parsed=parsed,
                        variables=variables,
                        ext=ext,
                        library_index=library_index,
                    )
                    wash_decision = wash_result.get("decision")
                    if wash_decision in ("keep_existing", "replace_existing"):
                        candidate = wash_result.get("candidate") or {}
                        new_info = wash_result.get("new_info") or {}
                        old_info = wash_result.get("old_info") or {}
                        logger.info(
                            "[Wash] 命中洗版候选: new=%s -> existing=%s | dir=%s | match=%s | "
                            "new_meta=%s/%s/%ss | old_meta=%s/%s/%ss",
                            file_name,
                            candidate.get("name", ""),
                            wash_result.get("candidate_dir", ""),
                            wash_result.get("match_reason", ""),
                            new_info.get("resource_pix", ""),
                            new_info.get("video_encode", ""),
                            int(float(new_info.get("duration_seconds") or 0)),
                            old_info.get("resource_pix", ""),
                            old_info.get("video_encode", ""),
                            int(float(old_info.get("duration_seconds") or 0)),
                        )
                    if wash_decision == "keep_existing":
                        wash_reason = wash_result.get("reason", "keep_existing")
                        logger.info(
                            "[Wash] 保留旧文件，跳过入库: %s | reason=%s | new_size=%.2fGB | old_size=%.2fGB | "
                            "new_eq=%.2f | old_eq=%.2f | new_gbph=%.2f | old_gbph=%.2f",
                            file_name,
                            wash_reason,
                            float(file_item.get("size", 0) or 0) / (1024 ** 3),
                            float((wash_result.get("candidate") or {}).get("size", 0) or 0) / (1024 ** 3),
                            float(wash_result.get("new_equivalent_size") or 0.0),
                            float(wash_result.get("old_equivalent_size") or 0.0),
                            float(wash_result.get("new_gbph") or 0.0),
                            float(wash_result.get("old_gbph") or 0.0),
                        )
                        results.append({
                            "file": file_name,
                            "status": "skipped",
                            "message": f"洗版未通过，保留旧文件（{wash_reason}）",
                        })
                        if wash_dir_cid:
                            pending_wash_reject_moves.append(file_item)
                            logger.debug(f"[Wash] 已暂存洗版未通过文件，整理结束后批量移走: {file_name}")
                        _processed = len(results)
                        _update_streaming_progress(
                            run_id,
                            scanned_video_count=scanned_video_count,
                            processed_result_count=_processed,
                            success_count=success_count,
                            error_count=_count_error_results(results),
                            strm_generated_count=strm_generated_count,
                            scan_complete=scan_complete,
                        )
                        _raise_if_organize_cancelled(run_id)
                        continue
                    if wash_decision == "replace_existing":
                        candidate = wash_result.get("candidate") or {}
                        removed, remove_reason = await loop.run_in_executor(
                            None,
                            lambda _c=client, _candidate=candidate, _failed=failed_dir_cid: _remove_library_candidate(_c, _candidate, _failed, _state._main_event_loop),
                        )
                        if not removed:
                            logger.warning(f"[Wash] 旧文件处理失败，跳过替换: {file_name} | reason={remove_reason}")
                            results.append({
                                "file": file_name,
                                "status": "error",
                                "message": f"洗版删除旧文件失败: {remove_reason}",
                            })
                            group_failed.append(file_item)
                            _processed = len(results)
                            _update_streaming_progress(
                                run_id,
                                scanned_video_count=scanned_video_count,
                                processed_result_count=_processed,
                                success_count=success_count,
                                error_count=_count_error_results(results),
                                strm_generated_count=strm_generated_count,
                                scan_complete=scan_complete,
                            )
                            _raise_if_organize_cancelled(run_id)
                            continue
                        candidate_path = str(candidate.get("path", "") or "")
                        candidate_id_str = str(candidate.get("id", "") or "")
                        cache_removed = False
                        if candidate_id_str:
                            cache_removed = bool(remove_task_item_by_id(
                                library_task_key,
                                candidate_id_str,
                                meta={"last_status": "updated_by_media_organize_wash_replace"},
                            ))
                        if (not cache_removed) and candidate_path:
                            remove_items_by_path_prefix(
                                library_task_key,
                                candidate_path,
                                meta={"last_status": "updated_by_media_organize_wash_replace"},
                            )
                        logger.info(
                            "[Wash] 新文件通过洗版，将替换旧文件: %s -> %s | reason=%s | new_size=%.2fGB | old_size=%.2fGB | "
                            "new_eq=%.2f | old_eq=%.2f | new_gbph=%.2f | old_gbph=%.2f",
                            file_name,
                            candidate.get('name', ''),
                            wash_result.get("reason", "replace_existing"),
                            float(file_item.get("size", 0) or 0) / (1024 ** 3),
                            float(candidate.get("size", 0) or 0) / (1024 ** 3),
                            float(wash_result.get("new_equivalent_size") or 0.0),
                            float(wash_result.get("old_equivalent_size") or 0.0),
                            float(wash_result.get("new_gbph") or 0.0),
                            float(wash_result.get("old_gbph") or 0.0),
                        )

                    if media_type == 'movie':
                        result = await loop.run_in_executor(None, lambda _c=client, _fi=file_item, _fn=file_name, _e=ext, _td=tmdb_data, _v=variables, _tc=effective_target_cid, _cd=config_data, _ow=req.overwrite, _cp=category_path or "", _tb=target_base, _sp=subtitles_by_parent, _ltk=library_task_key: _organize_movie(
                            _c, _fi, _fn, _e, _td, _v, _tc, _cd, _ow, category_path=_cp, target_path_base=_tb, subtitles_by_parent=_sp, main_loop=_state._main_event_loop, library_task_key=_ltk
                        ))
                        results.append({"file": file_name, "status": result["status"], "message": result.get("message", "")})
                        if result["status"] == "success":
                            success_count += 1
                            _send_organize_notify(_build_organize_notify_payload(
                                tmdb_data=tmdb_data,
                                variables=variables,
                                media_type=media_type,
                                tmdb_id=str(tmdb_id),
                                episodes=[(season_num, episode_num)],
                                success_count=1,
                                total_size=vf.get("size", 0),
                                elapsed_seconds=_time.time() - group_started_at,
                            ))
                            _finalize_organize_result(
                                result=result,
                                media_type=media_type,
                                vf=vf,
                                parsed=parsed,
                                variables=variables,
                                target_base=target_base,
                                category_path=category_path,
                                effective_target_cid=str(effective_target_cid),
                                library_task_key=library_task_key,
                                        library_index=library_index,
                                config_data=config_data,
                                metadata_executor=metadata_executor,
                                pending_library_cache_items=pending_library_cache_items,
                                pending_strm_payloads=pending_strm_payloads,
                                pending_emby_library_checks=pending_emby_library_checks,
                                pending_refresh_payloads=pending_refresh_payloads,
                            )
                            parent_id = str((vf or {}).get("parent_id", "") or "")
                            if parent_id:
                                organized_targets_by_parent.setdefault(parent_id, []).append({
                                    "file_item": vf,
                                    "renamed_file": result.get("renamed_file", "") or vf.get("name", ""),
                                    "target_cid": str((result.get("metadata_context") or {}).get("target_cid", "") or ""),
                                    "target_path": _join_remote_path(target_base, result.get("target_folder", "")),
                                })
                        else:
                            group_failed.append(file_item)
                    else:
                        folder_name, resolved_season_num, season_dir_name = _resolve_tv_target_names(variables, config_data, file_req)
                        batch_key = _build_tv_batch_key(
                            tmdb_data,
                            resolved_season_num,
                            effective_target_cid,
                            category_path or "",
                            target_base,
                            folder_name,
                            season_dir_name,
                        )
                        batch_context = pending_tv_batches.get(batch_key, {}).get("batch_context")
                        tv_root_key = (str(tmdb_id), str(effective_target_cid), target_base, folder_name)
                        scrape_tv_root = tv_root_key not in tv_root_scraped
                        scrape_season = batch_key not in season_scraped
                        tv_summary_key = str(tmdb_id)
                        should_log_tv_summary = tv_summary_key not in _tv_summary_logged
                        if batch_context is None:
                            batch_context = await loop.run_in_executor(None, lambda _c=client, _td=tmdb_data, _v=variables, _tc=effective_target_cid, _cd=config_data, _fr=file_req, _cp=category_path or "", _tb=target_base, _ltk=library_task_key: _build_tv_batch_context(
                                _c, _td, _v, _tc, _cd, _fr, category_path=_cp, target_path_base=_tb, library_task_key=_ltk
                            ))
                            if batch_context.get("status") != "planned":
                                if should_log_tv_summary:
                                    _tv_summary_logged.discard(tv_summary_key)
                                results.append({"file": file_name, "status": batch_context.get("status", "error"), "message": batch_context.get("message", "")})
                                group_failed.append(file_item)
                                continue
                            pending_tv_batches[batch_key] = {
                                "batch_context": batch_context,
                                "items": [],
                                "item_index": {},
                            }
                            if scrape_tv_root:
                                tv_root_scraped.add(tv_root_key)
                            if scrape_season:
                                season_scraped.add(batch_key)
                            if should_log_tv_summary:
                                _tv_summary_logged.add(tv_summary_key)
                        plan = _build_tv_episode_plan_from_context(
                            file_item,
                            file_name,
                            ext,
                            tmdb_data,
                            variables,
                            config_data,
                            file_req,
                            pending_tv_batches[batch_key]["batch_context"],
                            scrape_tv_root=scrape_tv_root,
                            scrape_season=scrape_season,
                            log_series_summary=should_log_tv_summary,
                        )
                        plan["vf"] = vf
                        plan["parsed"] = parsed
                        plan["variables"] = variables
                        plan["tmdb_id"] = str(tmdb_id)
                        plan["media_type"] = media_type
                        plan["season_num"] = season_num
                        plan["episode_num"] = episode_num
                        plan["target_base"] = target_base
                        plan["category_path"] = category_path
                        plan["effective_target_cid"] = str(effective_target_cid)

                        keep_incoming, existing_plan, dedupe_reason = await _dedupe_pending_tv_plan_item(
                            config_data,
                            drive_index,
                            pending_tv_batches,
                            batch_key,
                            plan,
                        )
                        if existing_plan is not None:
                            existing_name = str((existing_plan.get("vf") or {}).get("name", "") or "")
                            incoming_size_gb = float((vf or {}).get("size", 0) or 0) / (1024 ** 3)
                            existing_size_gb = float(((existing_plan.get("vf") or {}).get("size", 0) or 0)) / (1024 ** 3)
                            if keep_incoming:
                                pending_tv_batches[batch_key]["items"] = [
                                    item for item in pending_tv_batches[batch_key]["items"]
                                    if item is not existing_plan
                                ]
                                pending_tv_batches[batch_key]["item_index"].pop((season_num, episode_num), None)
                                logger.info(
                                    "[Wash] 同批次重复剧集命中，保留新文件: %s -> %s | S%02dE%02d | reason=%s | new_size=%.2fGB | old_size=%.2fGB",
                                    file_name,
                                    existing_name,
                                    int(season_num or 0),
                                    int(episode_num or 0),
                                    dedupe_reason,
                                    incoming_size_gb,
                                    existing_size_gb,
                                )
                                results.append({
                                    "file": existing_name,
                                    "status": "skipped",
                                    "message": f"同批次重复剧集，已被更优文件替换（{dedupe_reason}）",
                                })
                                if wash_dir_cid:
                                    await _move_top_dir_to_failed(
                                        client,
                                        existing_plan.get("vf") or {},
                                        str(source_cid),
                                        wash_dir_cid,
                                        moved_dirs,
                                        subtitles_by_parent=subtitles_by_parent,
                                    )
                            else:
                                logger.info(
                                    "[Wash] 同批次重复剧集命中，保留旧文件: %s -> %s | S%02dE%02d | reason=%s | new_size=%.2fGB | old_size=%.2fGB",
                                    file_name,
                                    existing_name,
                                    int(season_num or 0),
                                    int(episode_num or 0),
                                    dedupe_reason,
                                    incoming_size_gb,
                                    existing_size_gb,
                                )
                                results.append({
                                    "file": file_name,
                                    "status": "skipped",
                                    "message": f"同批次重复剧集，已保留更优文件（{dedupe_reason}）",
                                })
                                if wash_dir_cid:
                                    await _move_top_dir_to_failed(
                                        client,
                                        file_item,
                                        str(source_cid),
                                        wash_dir_cid,
                                        moved_dirs,
                                        subtitles_by_parent=subtitles_by_parent,
                                    )
                                continue

                        pending_tv_batches[batch_key]["items"].append(plan)
                        pending_tv_batches[batch_key]["item_index"][(season_num, episode_num)] = plan
                except Exception as e:
                    logger.error(f"[MediaOrganize] 整理文件失败 {file_name}: {e}", exc_info=True)
                    results.append({"file": file_name, "status": "error", "message": str(e)})
                    group_failed.append(file_item)

                # 进度更新与取消检查（每个文件处理完后）
                _processed = len(results)
                _update_streaming_progress(
                    run_id,
                    scanned_video_count=scanned_video_count,
                    processed_result_count=_processed,
                    success_count=success_count,
                    error_count=_count_error_results(results),
                    strm_generated_count=strm_generated_count,
                    scan_complete=scan_complete,
                )
                _raise_if_organize_cancelled(run_id)

            for batch_key, batch_entry in pending_tv_batches.items():
                plan_items = batch_entry.get("items", [])
                batch_results = await loop.run_in_executor(
                    None,
                    lambda _c=client, _items=plan_items, _sp=subtitles_by_parent: _execute_tv_batch_plan(_c, _items, subtitles_by_parent=_sp, main_loop=_state._main_event_loop),
                )
                batch_success = 0
                batch_size = 0
                batch_episodes = []
                batch_notify = None
                for plan_item, result in zip(plan_items, batch_results):
                    file_name = (plan_item.get("vf") or {}).get("name", "")
                    results.append({"file": file_name, "status": result.get("status", "error"), "message": result.get("message", "")})
                    if result.get("status") == "success":
                        success_count += 1
                        batch_success += 1
                        batch_size += (plan_item.get("vf") or {}).get("size", 0)
                        batch_episodes.append((plan_item.get("season_num"), plan_item.get("episode_num")))
                        if batch_notify is None:
                            batch_notify = (
                                plan_item.get("metadata_context", {}).get("tmdb_data") or {},
                                plan_item.get("variables") or {},
                                plan_item.get("media_type", "tv"),
                                plan_item.get("tmdb_id", ""),
                            )
                        _finalize_organize_result(
                            result=result,
                            media_type=plan_item.get("media_type", "tv"),
                            vf=plan_item.get("vf") or {},
                            parsed=plan_item.get("parsed") or {},
                            variables=plan_item.get("variables") or {},
                            target_base=plan_item.get("target_base", ""),
                            category_path=plan_item.get("category_path", ""),
                            effective_target_cid=plan_item.get("effective_target_cid", ""),
                            library_task_key=library_task_key,
                                library_index=library_index,
                            config_data=config_data,
                            metadata_executor=metadata_executor,
                            pending_library_cache_items=pending_library_cache_items,
                            pending_strm_payloads=pending_strm_payloads,
                            pending_emby_library_checks=pending_emby_library_checks,
                            pending_refresh_payloads=pending_refresh_payloads,
                        )
                        plan_vf = plan_item.get("vf") or {}
                        parent_id = str(plan_vf.get("parent_id", "") or "")
                        if parent_id:
                            organized_targets_by_parent.setdefault(parent_id, []).append({
                                "file_item": plan_vf,
                                "renamed_file": result.get("renamed_file", "") or plan_vf.get("name", ""),
                                "target_cid": str((result.get("metadata_context") or {}).get("season_cid", "") or ""),
                                "target_path": _join_remote_path(plan_item.get("target_base", ""), result.get("target_folder", ""), result.get("season_dir", "")),
                            })
                    else:
                        group_failed.append(plan_item.get("vf") or {})

                    _processed = len(results)
                    _update_streaming_progress(
                        run_id,
                        scanned_video_count=scanned_video_count,
                        processed_result_count=_processed,
                        success_count=success_count,
                        error_count=_count_error_results(results),
                        strm_generated_count=strm_generated_count,
                        scan_complete=scan_complete,
                    )
                    _raise_if_organize_cancelled(run_id)

                if batch_notify and batch_success > 0:
                    _td, _vars, _mt, _tid = batch_notify
                    _send_organize_notify(_build_organize_notify_payload(
                        tmdb_data=_td,
                        variables=_vars,
                        media_type=_mt,
                        tmdb_id=_tid,
                        episodes=batch_episodes,
                        success_count=batch_success,
                        total_size=batch_size,
                        elapsed_seconds=_time.time() - group_started_at,
                    ))

            logger.debug(
                f"[MediaOrganize] FFPROBE统计: 全量探测={ffprobe_stats['full_probe']} 样本={ffprobe_stats['sample']} 异常={ffprobe_stats['anomaly']} 文件缓存命中={ffprobe_stats['cache']} 批次缓存命中={ffprobe_stats['batch_cache']} 批次复用={ffprobe_stats['reuse']} 样本不一致={ffprobe_stats['mismatch']} 分段切换={ffprobe_stats['segment']}"
            )

            # 本组整理完：失败文件所在的顶层目录直接移到失败目录
            if group_failed and has_failed_dir and failed_dir_cid:
                await _move_failed_files_batch(
                    client, group_failed, str(source_cid),
                    failed_dir_cid, moved_dirs,
                    subtitles_by_parent=subtitles_by_parent,
                )

            # === 本组 STRM + 媒体库刷新 ===
            # 直接生成 STRM（组内完成，不提交到外部线程池）
            if pending_strm_payloads and config_data.get("auto_sync_strm", False):
                _flush_pending_library_cache_updates()
                strm_count = await loop.run_in_executor(
                    None, _generate_strm_batch_on_organize, list(pending_strm_payloads), config_data
                )
                strm_generated_count += strm_count
                logger.info(f"[MediaOrganize] 本组 STRM 生成完成: {strm_count} 个")

                # Emby 建库检查
                seen_checks = set()
                for payload in pending_emby_library_checks:
                    cp = str(payload.get("category_path", "") or "")
                    mt = str(payload.get("media_type", "") or "")
                    if not cp or cp == "其他":
                        continue
                    dk = (mt, cp)
                    if dk in seen_checks:
                        continue
                    seen_checks.add(dk)
                    try:
                        from app.services.emby_library_cache import ensure_library_if_needed
                        ensure_library_if_needed(cp, media_type=mt or None)
                    except Exception as e:
                        logger.debug(f"[EmbyLib] 建库检查失败: {e}")

            pending_strm_payloads.clear()
            pending_emby_library_checks.clear()

            _flush_pending_media_server_refreshes(immediate=True)

        prefetched_source_tree_entries = getattr(req, "_prefetched_source_tree_entries", None)
        has_prefetched_source_tree = prefetched_source_tree_entries is not None
        if has_prefetched_source_tree:
            prefetched_source_tree_entries = list(prefetched_source_tree_entries or [])
            logger.info(f"[MediaOrganize] 复用源目录稳定快照: 条目={len(prefetched_source_tree_entries)}")

        logger.debug("[MediaOrganize] 阶段4/4: 执行整理与刮削")
        _semaphore = asyncio.Semaphore(20)
        in_flight_group_tasks = []
        max_in_flight_group_tasks = 40
        grouped_items_by_key = {}

        async def _sem_search_and_organize(key, group):
            async with _semaphore:
                await _search_and_organize(key, group)

        def _submit_ready_group(group_key, group_items):
            if not group_key or not group_items:
                return False
            in_flight_group_tasks.append(asyncio.create_task(_sem_search_and_organize(group_key, list(group_items))))
            return True

        def _drain_finished_group_tasks():
            finished = [task for task in in_flight_group_tasks if task.done()]
            for task in finished:
                in_flight_group_tasks.remove(task)
                try:
                    task.result()
                except _OrganizeCancelledError:
                    raise
                except asyncio.CancelledError:
                    raise _OrganizeCancelledError("用户取消")
                except Exception as gather_result:
                    logger.error(f"[MediaOrganize] 分组整理协程异常: {type(gather_result).__name__}: {gather_result}", exc_info=gather_result)

        async def _yield_to_group_tasks(force_wait: bool = False):
            if pending_duplicate_moves:
                await _flush_pending_duplicate_moves()
            if not in_flight_group_tasks:
                return
            _raise_if_organize_cancelled(run_id)
            if force_wait or len(in_flight_group_tasks) >= max_in_flight_group_tasks:
                done, _ = await asyncio.wait(in_flight_group_tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    return
            else:
                await asyncio.sleep(0)
            _drain_finished_group_tasks()

        async def _wait_group_tasks_until_complete_or_cancel():
            while pending_duplicate_moves or in_flight_group_tasks:
                if pending_duplicate_moves:
                    await _flush_pending_duplicate_moves()
                _raise_if_organize_cancelled(run_id)
                if not in_flight_group_tasks:
                    break
                done, _ = await asyncio.wait(in_flight_group_tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    continue
                _drain_finished_group_tasks()

        media_entries_iter = (
            _iter_115_media_entries_from_tree(prefetched_source_tree_entries)
            if has_prefetched_source_tree
            else _iter_115_media_entries(client, str(source_cid))
        )
        for entry in media_entries_iter:
            kind = entry.get("kind")
            item = entry.get("item") or {}
            if kind == "subtitle":
                parent_id = str(item.get("parent_id", "") or "")
                subtitles_by_parent.setdefault(parent_id, []).append(item)
                if parent_id in organized_targets_by_parent:
                    moved_count = await _reconcile_late_subtitles(
                        client,
                        subtitles_by_parent,
                        {parent_id: organized_targets_by_parent.get(parent_id, [])},
                    )
                    if moved_count:
                        logger.info(f"[MediaOrganize] 晚到字幕补偿完成: parent={parent_id} count={moved_count}")
                continue
            if kind != "video":
                continue

            scanned_video_count += 1
            vf = item
            file_name = vf.get("name", "")
            ext = os.path.splitext(file_name)[1] or ".mkv"

            name_lower = file_name.lower()
            if any(kw in name_lower for kw in ("预告", "预告片", "trailer", "preview")):
                results.append({"file": file_name, "status": "skipped", "message": "预告片，跳过"})
                _update_streaming_progress(
                    run_id,
                    scanned_video_count=scanned_video_count,
                    processed_result_count=len(results),
                    success_count=success_count,
                    error_count=_count_error_results(results),
                    strm_generated_count=strm_generated_count,
                    scan_complete=False,
                )
                continue

            file_sha1 = vf.get("sha1", "").upper()
            if file_sha1 and library_index.has_sha1(file_sha1):
                logger.info(f"[MediaOrganize] SHA1已存在，跳过整理: {file_name}")
                results.append({
                    "file": file_name,
                    "status": "skipped",
                    "message": "目标目录已存在相同文件（SHA1 匹配），跳过",
                })
                if dedup_dir_cid:
                    pending_duplicate_moves.append({
                        "vf": vf,
                        "target_cid": str(dedup_dir_cid),
                        "file_op": {
                            "id": vf.get("id", 0),
                            "fid": vf.get("id", 0),
                            "name": file_name,
                            "old_name": file_name,
                            "new_name": file_name,
                            "path": vf.get("path", ""),
                            "source_path": vf.get("path", ""),
                            "pickcode": vf.get("pickcode", ""),
                        },
                    })
                    if len(pending_duplicate_moves) >= duplicate_batch_size:
                        await _flush_pending_duplicate_moves()
                _update_streaming_progress(
                    run_id,
                    scanned_video_count=scanned_video_count,
                    processed_result_count=len(results),
                    success_count=success_count,
                    error_count=_count_error_results(results),
                    strm_generated_count=strm_generated_count,
                    scan_complete=False,
                )
                continue

            file_path = vf.get("path", "")
            parsed = _parse_filename(file_name, media_type_hint=req.media_type or None, file_path=file_path)
            if not parsed:
                logger.info(f"[MediaOrganize] 解析失败 -> {file_name}")
                results.append({"file": file_name, "status": "error", "message": "无法识别媒体信息"})
                _update_streaming_progress(
                    run_id,
                    scanned_video_count=scanned_video_count,
                    processed_result_count=len(results),
                    success_count=success_count,
                    error_count=_count_error_results(results),
                    strm_generated_count=strm_generated_count,
                    scan_complete=False,
                )
                continue

            logger.info(
                f"[MediaOrganize] 解析成功 -> {file_name} | 标题:{parsed.get('title','')} 年份:{parsed.get('year','')} 类型:{parsed.get('media_type','')} 季:{parsed.get('season')} 集:{parsed.get('episode')}"
            )
            key = parsed["group_key"]
            grouped_items_by_key.setdefault(key, []).append((vf, parsed, ext))
            _update_streaming_progress(
                run_id,
                scanned_video_count=scanned_video_count,
                processed_result_count=len(results),
                success_count=success_count,
                error_count=_count_error_results(results),
                strm_generated_count=strm_generated_count,
                scan_complete=False,
            )
            if scanned_video_count % 50 == 0:
                await _yield_to_group_tasks()
            _raise_if_organize_cancelled(run_id)

        if scanned_video_count == 0:
            try:
                await _cleanup_empty_source_dirs(client, str(source_cid))
            except Exception as e:
                logger.warning(f"[MediaOrganize] 清理空文件夹失败: {e}")
            update_task_progress(run_id, "整理: 源目录没有视频文件", 100, "finished")
            return

        if pending_duplicate_moves:
            await _flush_pending_duplicate_moves()

        scan_complete = True
        total_files = scanned_video_count
        logger.info(f"[MediaOrganize] 扫描完成，按媒体聚合为 {len(grouped_items_by_key)} 组")
        for group_key, group_items in grouped_items_by_key.items():
            submitted = _submit_ready_group(group_key, group_items)
            if submitted:
                await _yield_to_group_tasks()
        if in_flight_group_tasks:
            await _wait_group_tasks_until_complete_or_cancel()

        _raise_if_organize_cancelled(run_id)
        if pending_wash_reject_moves:
            await _flush_pending_wash_reject_moves()
            _raise_if_organize_cancelled(run_id)

        compensated_count = await _reconcile_late_subtitles(
            client,
            subtitles_by_parent,
            organized_targets_by_parent,
        )
        if compensated_count:
            logger.info(f"[MediaOrganize] 扫描结束后完成晚到字幕补偿: {compensated_count} 个字幕")

        _flush_pending_library_cache_updates()

        _flush_pending_media_server_refreshes()

        metadata_executor.shutdown(wait=True)

        failed_count = sum(1 for r in results if r.get("status") == "error")
        skipped_count = sum(1 for r in results if r.get("status") == "skipped")
        sha1_duplicate_skipped_count = sum(
            1 for r in results
            if r.get("status") == "skipped" and "SHA1 匹配" in str(r.get("message", ""))
        )
        other_skipped_count = max(0, skipped_count - sha1_duplicate_skipped_count)
        failed_results = [r for r in results if r.get("status") == "error"]
        elapsed = _time.time() - _org_start
        logger.info(
            f"[MediaOrganize] 整理完成: 成功 {success_count}/{total_files} | 失败 {failed_count} | "
            f"跳过 {skipped_count} (SHA1重复 {sha1_duplicate_skipped_count}, 其他 {other_skipped_count}) | "
            f"新生成STRM {strm_generated_count} | 耗时 {elapsed:.1f}s"
        )
        if failed_results:
            logger.warning(f"[MediaOrganize] 本次失败文件明细: {len(failed_results)} 个")
            for idx, failed in enumerate(failed_results[:20], 1):
                failed_file = str(failed.get("file", "") or "未知文件")
                failed_reason = str(failed.get("message", "") or "未知原因")
                logger.warning(f"[MediaOrganize] 失败 {idx}/{len(failed_results)}: {failed_file} | 原因: {failed_reason}")
            if len(failed_results) > 20:
                logger.warning(f"[MediaOrganize] 失败文件还有 {len(failed_results) - 20} 个未在摘要中展开")

        try:
            await _cleanup_empty_source_dirs(client, str(source_cid))
        except Exception as e:
            logger.warning(f"[MediaOrganize] 清理空文件夹失败: {e}")

        source_dir = str(config_data.get("source_name", "") or "")
        target_dir = str(config_data.get("target_name", "") or "")
        _schedule_or_refresh_source_poll(
            drive_index=drive_index,
            source_dir=source_dir,
            target_dir=target_dir,
            source_cid=str(source_cid),
            phase="post_run",
        )

        update_task_progress(run_id, f"整理完成: {success_count}/{total_files} 成功", 100, "finished")
        ACTIVE_TASKS[run_id]["detail"] = {
            "total": total_files,
            "success": success_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "sha1_duplicate_skipped": sha1_duplicate_skipped_count,
            "other_skipped": other_skipped_count,
            "strm": strm_generated_count,
        }

    except _OrganizeCancelledError:
        for task in list(locals().get("in_flight_group_tasks", []) or []):
            if not task.done():
                task.cancel()
        try:
            if metadata_executor:
                metadata_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        _processed = len(results)
        _failed = _count_error_results(results)
        update_task_progress(run_id, f"整理已取消: 已处理 {_processed}/{scanned_video_count}", 100, "stopped")
        ACTIVE_TASKS[run_id]["detail"] = {
            "total": scanned_video_count, "success": success_count, "failed": _failed, "strm": strm_generated_count,
        }
        logger.info(f"[MediaOrganize] 整理已取消: 成功 {success_count}/{scanned_video_count}")

    except Exception as e:
        try:
            _flush_pending_library_cache_updates()
        except Exception as flush_err:
            logger.error(f"[MediaOrganize] 异常退出前批量写缓存失败: {flush_err}")
        try:
            if metadata_executor:
                metadata_executor.shutdown(wait=False)
        except Exception:
            pass
        logger.error(f"[MediaOrganize] 整理失败: {e}", exc_info=True)
        update_task_progress(run_id, f"整理失败: {e}", 0, "error")


# ---------------------------------------------------------------------------
# _organize_movie  /  _organize_tv
# ---------------------------------------------------------------------------

def _build_organize_notify_payload(*, tmdb_data: dict, variables: dict, media_type: str, tmdb_id: str,
                                  episodes: list[tuple], success_count: int, total_size: int,
                                  elapsed_seconds: float) -> dict:
    source = tmdb_data.get("series_details") if "series_details" in tmdb_data else tmdb_data
    title = source.get("name" if media_type == "tv" else "title", "")
    eps = sorted(set(e for _, e in episodes if e is not None))
    seas = sorted(set(s for s, _ in episodes if s is not None))
    season_episode = ""
    if media_type == "tv":
        s = f"S{seas[0]:02d}" if seas else ""
        e = (f"E{eps[0]:02d}" if len(eps) == 1 else f"E{eps[0]:02d}-E{eps[-1]:02d}") if eps else ""
        season_episode = s + e
    vote = source.get("vote_average", 0)
    file_size = (
        f"{total_size/1024**3:.2f}G" if total_size >= 1024**3 else
        f"{total_size/1024**2:.0f}M" if total_size >= 1024**2 else
        f"{total_size/1024:.0f}K" if total_size else ""
    )
    quality = " ".join(p for p in [variables.get("source", ""), variables.get("resource_effect", ""), variables.get("video_effect", ""), variables.get("resource_pix", "")] if p)
    audio = " ".join(p for p in [variables.get("video_encode", ""), variables.get("color_depth", ""), variables.get("fps", ""), variables.get("audio_encode", "")] if p)
    return {
        "media_name": title,
        "media_type": media_type,
        "year": variables.get("year", ""),
        "season_episode": season_episode,
        "rating": f"{vote:.1f}分" if vote else "",
        "genres": " · ".join(g.get("name", "") for g in source.get("genres", [])[:3]),
        "overview": (source.get("overview", "") or "")[:150],
        "tmdb_id": tmdb_id,
        "quality": quality,
        "audio": audio,
        "episode_count": str(success_count) if media_type == "tv" else "",
        "file_size": file_size,
        "release_group": variables.get("release_group", ""),
        "elapsed": f"{elapsed_seconds:.1f}秒",
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "original_name": source.get("original_name" if media_type == "tv" else "original_title", ""),
    }


def _send_organize_notify(payload: dict):
    if not payload:
        return
    from app.services.wechat_service import wechat_notify_service
    from app.services.telegram_service import telegram_notify_service
    wechat_notify_service.notify_organize_complete(**payload)
    telegram_notify_service.notify_organize_complete(**payload)


def _finalize_organize_result(
    *, result: dict, media_type: str, vf: dict, parsed: dict, variables: dict,
    target_base: str, category_path: str, effective_target_cid: str, library_task_key: str,
    library_index, config_data: dict,
    metadata_executor, pending_library_cache_items: dict,
    pending_strm_payloads: list, pending_emby_library_checks: list, pending_refresh_payloads: list,
):
    file_sha1 = vf.get("sha1", "").upper()

    def _queue_cache_item(item_key: str, item_data: dict):
        normalized_key = str(item_key or "")
        if not normalized_key:
            return
        normalized_item = {
            "name": str(item_data.get("name", "") or ""),
            "path": str(item_data.get("path", "") or ""),
            "pickcode": str(item_data.get("pickcode", "") or ""),
            "size": int(item_data.get("size", 0) or 0),
            "id": int(item_data.get("id", 0) or 0),
            "sha1": str(item_data.get("sha1", "") or ""),
            "is_dir": bool(item_data.get("is_dir", False)),
            "parent_id": int(item_data.get("parent_id", 0) or 0),
        }
        pending_library_cache_items[normalized_key] = normalized_item
        library_index.add_or_update_items({normalized_key: normalized_item})

    meta_ctx = result.get("metadata_context") or {}
    target_folder_name = result.get("target_folder", "")
    season_dir_name = result.get("season_dir", "")
    renamed_file = result.get("renamed_file", "") or vf.get("name", "")
    target_file_path = _join_remote_path(target_base, target_folder_name, season_dir_name, renamed_file)
    file_season_cid = meta_ctx.get("season_cid", "")
    file_folder_cid = meta_ctx.get("target_cid", "")
    file_parent_id = int(file_season_cid) if file_season_cid and str(file_season_cid).isdigit() else (
        int(file_folder_cid) if file_folder_cid and str(file_folder_cid).isdigit() else 0
    )
    _queue_cache_item(
        str(vf.get("id", "") or ""),
        {
            "name": renamed_file,
            "path": target_file_path,
            "pickcode": vf.get("pickcode", ""),
            "size": vf.get("size", 0),
            "id": vf.get("id", 0),
            "sha1": file_sha1,
            "is_dir": False,
            "parent_id": file_parent_id,
        },
    )

    folder_cid = str(meta_ctx.get("target_cid", "") or "")
    folder_name_val = str(meta_ctx.get("folder_name", "") or "")
    folder_pickcode_val = str(meta_ctx.get("folder_pickcode", "") or "")
    effective_target_parent_id = int(effective_target_cid) if str(effective_target_cid).isdigit() else 0

    if media_type != "tv" and folder_cid and folder_name_val:
        folder_path = _join_remote_path(target_base, folder_name_val)
        _queue_cache_item(
            folder_cid,
            {
                "name": folder_name_val,
                "path": folder_path,
                "pickcode": folder_pickcode_val,
                "size": 0,
                "id": int(folder_cid) if folder_cid.isdigit() else 0,
                "sha1": "",
                "is_dir": True,
                "parent_id": effective_target_parent_id,
            },
        )

    if media_type == "tv":
        season_cid_val = str(meta_ctx.get("season_cid", "") or "")
        season_pickcode_val = str(meta_ctx.get("season_pickcode", "") or "")
        season_dir_val = str(result.get("season_dir", "") or "")
        series_name = str(result.get("target_folder", "") or "")
        if folder_cid and series_name:
            series_path = _join_remote_path(target_base, series_name)
            _queue_cache_item(
                folder_cid,
                {
                    "name": series_name,
                    "path": series_path,
                    "pickcode": folder_pickcode_val,
                    "size": 0,
                    "id": int(folder_cid) if folder_cid.isdigit() else 0,
                    "sha1": "",
                    "is_dir": True,
                    "parent_id": effective_target_parent_id,
                },
            )
        if season_cid_val and season_dir_val:
            season_path = _join_remote_path(target_base, series_name, season_dir_val)
            _queue_cache_item(
                season_cid_val,
                {
                    "name": season_dir_val,
                    "path": season_path,
                    "pickcode": season_pickcode_val,
                    "size": 0,
                    "id": int(season_cid_val) if season_cid_val.isdigit() else 0,
                    "sha1": "",
                    "is_dir": True,
                    "parent_id": int(folder_cid) if folder_cid.isdigit() else 0,
                },
            )

    if config_data.get("auto_sync_strm", False):
        strm_ctx = result.get("strm_context") or {}
        if strm_ctx:
            pending_strm_payloads.append({
                "result": {
                    "renamed_file": result.get("renamed_file", ""),
                    "target_folder": result.get("target_folder", ""),
                    "season_dir": result.get("season_dir", ""),
                    "moved_subtitles": result.get("moved_subtitles", []),
                    "size": vf.get("size", 0),
                    "id": vf.get("id", 0),
                    "sha1": vf.get("sha1", ""),
                },
                "media_type": strm_ctx.get("media_type", media_type),
                "pickcode": strm_ctx.get("pickcode", ""),
                "category_path": strm_ctx.get("category_path", ""),
            })

    if category_path and category_path != "其他":
        pending_emby_library_checks.append({
            "category_path": category_path,
            "media_type": media_type,
        })

    if meta_ctx and config_data.get("scrape_enabled", True):
        future = metadata_executor.submit(
            _process_metadata_task,
            config_data,
            meta_ctx,
        )

        def _handle_metadata_error(f):
            try:
                exc = f.exception()
            except Exception as e:
                logger.debug(f"[MediaOrganize] 元数据任务状态读取失败: {e}")
                return
            if exc:
                logger.debug(f"[MediaOrganize] 元数据任务失败: {exc}")

        future.add_done_callback(_handle_metadata_error)

    target_name = config_data.get("target_name", "")
    cat = f"/{category_path}" if category_path and category_path != "其他" else ""
    if media_type == "movie":
        remote_refresh_path = f"{target_name}{cat}/{result.get('target_folder', '')}"
    else:
        remote_refresh_path = f"{target_name}{cat}/{result.get('target_folder', '')}/{result.get('season_dir', '')}"
    refresh_target_path = _map_remote_to_strm_local_path(remote_refresh_path)

    pending_refresh_payloads.append({
        "title": variables.get("title") or parsed.get("title", ""),
        "year": variables.get("year", ""),
        "type": media_type,
        "category": category_path or "",
        "target_path": refresh_target_path,
    })


def _resolve_tv_target_names(variables: dict, config_data: dict, req) -> tuple[str, int, str]:
    folder_format = config_data.get("tv_folder_format", "{title} ({year}) [tmdbid-{tmdb_id}]")
    folder_name = _render_template(folder_format, variables)
    season_num = req.season_number if req.season_number is not None else 1
    season_dir_name = f"Season {season_num:02d}"
    return folder_name, season_num, season_dir_name



def _build_tv_batch_key(tmdb_data: dict, season_num: int, target_cid, category_path: str,
                        target_path_base: str, folder_name: str, season_dir_name: str) -> tuple:
    return (
        str((tmdb_data.get("series_details") or tmdb_data).get("id") or ""),
        season_num,
        str(target_cid),
        category_path or "",
        target_path_base or "",
        folder_name,
        season_dir_name,
    )



def _build_tv_batch_context(client, tmdb_data, variables, target_cid, config_data, req,
                            category_path="", target_path_base="", library_task_key: str = ""):
    """构建同剧同季共享的上下文，只做一次目录准备。"""
    folder_name, season_num, season_dir_name = _resolve_tv_target_names(variables, config_data, req)
    if not folder_name:
        return {"status": "error", "message": "无法生成目标目录名"}

    full_path = f"{folder_name}/{season_dir_name}"
    season_cid = None
    series_cid = None
    series_pickcode = ""
    season_pickcode = ""
    try:
        current_parent_cid = str(target_cid)
        current_dir_path = str(target_path_base or "").rstrip("/")
        for segment in [folder_name, season_dir_name]:
            current_dir_path = f"{current_dir_path}/{segment}" if current_dir_path else segment
            created_cid, created_pickcode = _mkdir_115_dir(
                client,
                current_parent_cid,
                segment,
                task_key=library_task_key,
                dir_path=current_dir_path,
            )
            current_parent_cid = created_cid
            if segment == folder_name:
                series_cid = created_cid
                series_pickcode = created_pickcode
            else:
                season_cid = created_cid
                season_pickcode = created_pickcode
    except Exception as e:
        logger.warning(f"[MediaOrganize] 创建目录链失败: {full_path}, {e}")
    if not season_cid:
        return {"status": "error", "message": f"创建目录失败: {full_path}"}

    series_dir_path = f"{target_path_base.rstrip('/')}/{folder_name}" if target_path_base else folder_name
    season_dir_path = f"{series_dir_path}/{season_dir_name}"

    return {
        "status": "planned",
        "target_folder": folder_name,
        "season_dir": season_dir_name,
        "season_num": season_num,
        "series_cid": str(series_cid),
        "series_pickcode": series_pickcode,
        "season_cid": str(season_cid) if season_cid else "",
        "season_pickcode": season_pickcode if season_cid else "",
        "series_dir_path": series_dir_path,
        "season_dir_path": season_dir_path,
        "category_path": category_path,
        "target_path_base": target_path_base,
        "batch_key": _build_tv_batch_key(
            tmdb_data,
            season_num,
            target_cid,
            category_path,
            target_path_base,
            folder_name,
            season_dir_name,
        ),
    }



def _build_tv_episode_plan_from_context(file_item, file_name, ext, tmdb_data, variables, config_data, req,
                                        batch_context: dict, scrape_tv_root=True, scrape_season=True,
                                        log_series_summary=True):
    episode_format = config_data.get("tv_episode_format", "{en_title}.{season_episode}.{year}.{resource_pix}.{web_source}.{resource_type}.{video_encode}.{color_depth}.{video_effect}.{fps}.{audio_encode}-{resource_team}")
    new_name = _render_template(episode_format, variables) + ext
    resource_vars = {k: v for k, v in variables.items()
                     if k in ("resource_pix", "video_encode", "audio_encode", "resource_type",
                               "resource_effect", "video_effect", "web_source", "fps", "en_title", "source",
                               "release_group", "color_depth")}
    logger.debug(f"[MediaOrganize] 剧集重命名计划: {file_name!r} -> {new_name!r}")
    logger.debug(f"[MediaOrganize] 资源字段: { {k: v for k, v in resource_vars.items() if v} }")
    if new_name == file_name:
        empty_fields = [k for k, v in resource_vars.items() if not v]
        logger.debug(f"[MediaOrganize] 文件名未变化，仅移动: {file_name!r} (空字段: {empty_fields})")

    return {
        "status": "planned",
        "target_folder": batch_context.get("target_folder", ""),
        "season_dir": batch_context.get("season_dir", ""),
        "renamed_file": new_name,
        "metadata_context": {
            "tmdb_data": tmdb_data,
            "media_type": "tv",
            "target_cid": batch_context.get("series_cid", ""),
            "folder_pickcode": batch_context.get("series_pickcode", ""),
            "season_cid": batch_context.get("season_cid", ""),
            "season_pickcode": batch_context.get("season_pickcode", ""),
            "overwrite": req.overwrite,
            "season_number": batch_context.get("season_num"),
            "episode_number": req.episode_number,
            "nfo_stem": os.path.splitext(new_name)[0],
            "summary_title": variables.get("title", ""),
            "summary_year": variables.get("year", ""),
            "folder_name": batch_context.get("target_folder", ""),
            "category_path": batch_context.get("category_path", ""),
            "scrape_tv_root": scrape_tv_root,
            "scrape_season": scrape_season,
            "log_series_summary": log_series_summary,
        },
        "strm_context": {
            "media_type": "tv",
            "pickcode": file_item.get("pickcode", ""),
            "category_path": batch_context.get("category_path", ""),
        },
        "batch_key": batch_context.get("batch_key"),
        "season_dir_path": batch_context.get("season_dir_path", ""),
        "series_dir_path": batch_context.get("series_dir_path", ""),
        "file_op": {
            "id": file_item.get("id"),
            "fid": file_item.get("id"),
            "name": file_name,
            "old_name": file_name,
            "new_name": new_name,
            "path": file_item.get("path", ""),
            "source_path": file_item.get("path", ""),
        },
    }



def _build_tv_organize_plan(client, file_item, file_name, ext, tmdb_data,
                            variables, target_cid, config_data, req,
                            scrape_tv_root=True, scrape_season=True, category_path="", target_path_base="",
                            log_series_summary=True, library_task_key: str = ""):
    """构建剧集整理计划，不执行文件操作。"""
    batch_context = _build_tv_batch_context(
        client,
        tmdb_data,
        variables,
        target_cid,
        config_data,
        req,
        category_path=category_path,
        target_path_base=target_path_base,
        library_task_key=library_task_key,
    )
    if batch_context.get("status") != "planned":
        return batch_context
    return _build_tv_episode_plan_from_context(
        file_item,
        file_name,
        ext,
        tmdb_data,
        variables,
        config_data,
        req,
        batch_context,
        scrape_tv_root=scrape_tv_root,
        scrape_season=scrape_season,
        log_series_summary=log_series_summary,
    )


def _execute_tv_batch_plan(client, plan_items: list[dict], subtitles_by_parent=None,
                           main_loop: Optional[asyncio.AbstractEventLoop] = None) -> list[dict]:
    if not plan_items:
        return []

    if main_loop is None:
        raise RuntimeError("缺少主事件循环，无法执行异步重命名/移动")

    batch_result = _await_on_main_loop(
        _rename_115_files_batch(
            client,
            [item.get("file_op", {}) for item in plan_items],
            target_cid=str((plan_items[0].get("metadata_context") or {}).get("season_cid", "") or ""),
            target_path=str(plan_items[0].get("season_dir_path", "") or ""),
        ),
        main_loop,
    )

    if batch_result.get("ok"):
        subtitle_result_map = {}
        if subtitles_by_parent:
            subtitle_plans = []
            for item in plan_items:
                subtitle_plans.append({
                    "video_id": str((item.get("vf") or {}).get("id", "") or ""),
                    "file_item": item.get("vf") or {},
                    "video_new_name": item.get("renamed_file", ""),
                })
            subtitle_result_map = _await_on_main_loop(
                _match_and_move_subtitles_batch(
                    client,
                    subtitle_plans,
                    subtitles_by_parent,
                    target_cid=str((plan_items[0].get("metadata_context") or {}).get("season_cid", "") or ""),
                    target_path=str(plan_items[0].get("season_dir_path", "") or ""),
                ),
                main_loop,
            )

        executed = []
        for item in plan_items:
            video_id = str((item.get("vf") or {}).get("id", "") or "")
            result = {
                "status": "success",
                "message": f"剧集整理完成: {item.get('target_folder', '')} / {item.get('season_dir', '')}",
                "target_folder": item.get("target_folder", ""),
                "season_dir": item.get("season_dir", ""),
                "renamed_file": item.get("renamed_file", ""),
                "metadata_context": item.get("metadata_context", {}),
                "strm_context": item.get("strm_context", {}),
            }
            _record_organized_source_path(video_id, item.get("season_dir_path", ""))
            result["moved_subtitles"] = subtitle_result_map.get(video_id, [])
            executed.append(result)
        return executed

    executed = []
    rename_done = bool(batch_result.get("rename_done"))
    move_done = bool(batch_result.get("move_done"))
    for item in plan_items:
        file_item = item.get("vf") or {}
        new_name = item.get("renamed_file", "")
        current_name = new_name if rename_done else file_item.get("name", "")
        file_for_fallback = dict(file_item)
        file_for_fallback["name"] = current_name
        target_cid = None if move_done else str((item.get("metadata_context") or {}).get("season_cid", "") or "")
        fallback_name = "" if rename_done else new_name
        ok = _await_on_main_loop(
            _rename_115_file(
                client,
                file_for_fallback,
                fallback_name,
                target_cid=target_cid,
                target_path=item.get("season_dir_path", ""),
            ),
            main_loop,
        )
        if not ok:
            executed.append({
                "status": "error",
                "message": f"文件移动失败: {file_item.get('name', '')}",
            })
            continue
        _record_organized_source_path(str(file_item.get("id", "") or ""), item.get("season_dir_path", ""))
        moved_subtitles = []
        if subtitles_by_parent:
            moved_subtitles = _await_on_main_loop(
                _match_and_move_subtitles(
                    client,
                    file_item,
                    new_name,
                    subtitles_by_parent,
                    target_cid=str((item.get("metadata_context") or {}).get("season_cid", "") or ""),
                    target_path=item.get("season_dir_path", ""),
                ),
                main_loop,
            )
        executed.append({
            "status": "success",
            "message": f"剧集整理完成: {item.get('target_folder', '')} / {item.get('season_dir', '')}",
            "target_folder": item.get("target_folder", ""),
            "season_dir": item.get("season_dir", ""),
            "renamed_file": new_name,
            "moved_subtitles": moved_subtitles,
            "metadata_context": item.get("metadata_context", {}),
            "strm_context": item.get("strm_context", {}),
        })
    return executed


def _execute_duplicate_batch_plan(client, plan_items: list[dict], subtitles_by_parent=None,
                                  main_loop: Optional[asyncio.AbstractEventLoop] = None) -> list[dict]:
    if not plan_items:
        return []

    if main_loop is None:
        raise RuntimeError("缺少主事件循环，无法执行异步重命名/移动")

    target_cid = str(plan_items[0].get("target_cid", "") or "")
    batch_result = _await_on_main_loop(
        _rename_115_files_batch(
            client,
            [item.get("file_op", {}) for item in plan_items],
            target_cid=target_cid,
            target_path="",
        ),
        main_loop,
    )

    if batch_result.get("ok"):
        subtitle_result_map = {}
        if subtitles_by_parent:
            subtitle_result_map = _await_on_main_loop(
                _match_and_move_subtitles_batch(
                    client,
                    [
                        {
                            "video_id": str((item.get("vf") or {}).get("id", "") or ""),
                            "file_item": item.get("vf") or {},
                            "video_new_name": str((item.get("vf") or {}).get("name", "") or ""),
                        }
                        for item in plan_items
                    ],
                    subtitles_by_parent,
                    target_cid=target_cid,
                    target_path="",
                    preserve_subtitle_name=True,
                ),
                main_loop,
            )

        executed = []
        for item in plan_items:
            vf = item.get("vf") or {}
            file_id = str(vf.get("id", "") or "")
            _record_organized_source_path(file_id, "")
            executed.append({
                "status": "success",
                "file": str(vf.get("name", "") or ""),
                "moved_subtitles": subtitle_result_map.get(file_id, []),
            })
        return executed

    executed = []
    rename_done = bool(batch_result.get("rename_done"))
    move_done = bool(batch_result.get("move_done"))
    for item in plan_items:
        vf = item.get("vf") or {}
        file_name = str(vf.get("name", "") or "")
        file_for_fallback = dict(vf)
        target_cid_for_fallback = None if move_done else target_cid
        fallback_name = "" if rename_done else file_name
        ok = _await_on_main_loop(
            _rename_115_file(
                client,
                file_for_fallback,
                fallback_name,
                target_cid=target_cid_for_fallback,
                target_path="",
            ),
            main_loop,
        )
        if not ok:
            executed.append({
                "status": "error",
                "file": file_name,
                "message": f"重复文件移动失败: {file_name}",
            })
            continue
        _record_organized_source_path(str(vf.get("id", "") or ""), "")
        moved_subtitles = []
        if subtitles_by_parent:
            moved_subtitles = _await_on_main_loop(
                _move_matched_subtitles_to_target(
                    client,
                    vf,
                    subtitles_by_parent,
                    target_cid=target_cid,
                    target_path="",
                ),
                main_loop,
            )
        executed.append({
            "status": "success",
            "file": file_name,
            "moved_subtitles": moved_subtitles,
        })
    return executed


def _organize_movie(client, file_item, file_name, ext, tmdb_data,
                    variables, target_cid, config_data, overwrite, category_path="", target_path_base="",
                    subtitles_by_parent=None, main_loop: Optional[asyncio.AbstractEventLoop] = None,
                    library_task_key: str = ""):
    """整理单部电影"""
    # 渲染目标目录名
    folder_format = config_data.get("movie_folder_format", "{title} ({year}) {tmdb-{tmdb_id}}")
    folder_name = _render_template(folder_format, variables)

    if not folder_name:
        return {"status": "error", "message": "无法生成目标目录名"}

    if main_loop is None:
        raise RuntimeError("缺少主事件循环，无法执行异步重命名/移动")

    # 在 target_cid 下创建电影目录
    dir_cid = None
    dir_pickcode = ""
    target_dir_path = f"{target_path_base.rstrip('/')}/{folder_name}" if target_path_base else folder_name
    try:
        dir_cid, dir_pickcode = _mkdir_115_dir(
            client,
            str(target_cid),
            folder_name,
            task_key=library_task_key,
            dir_path=target_dir_path,
        )
    except Exception as e:
        logger.warning(f"[MediaOrganize] 创建目录失败: {folder_name}, {e}")
    if not dir_cid:
        return {"status": "error", "message": f"创建目录失败: {folder_name}"}

    # 重命名 + 移动
    rename_format = config_data.get("movie_rename_format", "{en_title}.{year}.{resource_pix}.{web_source}.{resource_type}.{resource_effect}.{video_encode}.{color_depth}.{video_effect}.{fps}.{audio_encode}-{resource_team}")
    new_name = _render_template(rename_format, variables) + ext
    resource_vars = {k: v for k, v in variables.items()
                     if k in ("resource_pix", "video_encode", "audio_encode", "resource_type",
                               "resource_effect", "video_effect", "web_source", "fps", "en_title", "source",
                               "release_group", "color_depth")}
    logger.debug(f"[MediaOrganize] 电影重命名: {file_name!r} -> {new_name!r}")
    logger.debug(f"[MediaOrganize] 资源字段: { {k: v for k, v in resource_vars.items() if v} }")
    if new_name and new_name != file_name:
        ok = _await_on_main_loop(
            _rename_115_file(client, file_item, new_name, target_cid=str(dir_cid), target_path=target_dir_path),
            main_loop,
        )
    else:
        if new_name == file_name:
            empty_fields = [k for k, v in resource_vars.items() if not v]
            logger.debug(f"[MediaOrganize] 文件名未变化，仅移动: {file_name!r} (空字段: {empty_fields})")
        ok = _await_on_main_loop(
            _rename_115_file(client, file_item, "", target_cid=str(dir_cid), target_path=target_dir_path),
            main_loop,
        )
    if not ok:
        return {"status": "error", "message": f"文件移动失败: {file_name}"}

    _record_organized_source_path(str(file_item.get("id", "") or ""), target_dir_path)

    # 移动匹配的字幕文件到目标目录
    moved_subtitles = []
    if subtitles_by_parent:
        moved_subtitles = _await_on_main_loop(
            _match_and_move_subtitles(client, file_item, new_name, subtitles_by_parent,
                                      target_cid=str(dir_cid), target_path=target_dir_path),
            main_loop,
        )

    return {
        "status": "success",
        "message": f"电影整理完成: {folder_name}",
        "target_folder": folder_name,
        "moved_subtitles": moved_subtitles,
        "renamed_file": new_name,
        "metadata_context": {
            "tmdb_data": tmdb_data,
            "media_type": "movie",
            "target_cid": str(dir_cid),
            "folder_pickcode": dir_pickcode,
            "overwrite": overwrite,
            "nfo_stem": os.path.splitext(new_name)[0],
            "summary_title": variables.get("title", ""),
            "summary_year": variables.get("year", ""),
            "folder_name": folder_name,
            "category_path": category_path,
        },
        "strm_context": {
            "media_type": "movie",
            "pickcode": file_item.get("pickcode", ""),
            "category_path": category_path,
        },
    }


def _organize_tv(client, file_item, file_name, ext, tmdb_data,
                  variables, target_cid, config_data, req,
                  scrape_tv_root=True, scrape_season=True, category_path="", target_path_base="",
                  log_series_summary=True, subtitles_by_parent=None,
                  main_loop: Optional[asyncio.AbstractEventLoop] = None,
                  library_task_key: str = ""):
    """兼容旧调用的单集剧集整理封装。"""
    plan = _build_tv_organize_plan(
        client,
        file_item,
        file_name,
        ext,
        tmdb_data,
        variables,
        target_cid,
        config_data,
        req,
        scrape_tv_root=scrape_tv_root,
        scrape_season=scrape_season,
        category_path=category_path,
        target_path_base=target_path_base,
        log_series_summary=log_series_summary,
        library_task_key=library_task_key,
    )
    if plan.get("status") != "planned":
        return plan
    plan["vf"] = file_item
    executed = _execute_tv_batch_plan(client, [plan], subtitles_by_parent=subtitles_by_parent, main_loop=main_loop)
    return executed[0] if executed else {"status": "error", "message": f"文件移动失败: {file_name}"}


# ---------------------------------------------------------------------------
# create_life_event_callback
# ---------------------------------------------------------------------------

def create_life_event_callback(
    source_dir: str,
    drive_index: int = 0,
    target_dir: str = "",
    source_cid: str = "",
    target_cid: str = "",
) -> Callable:
    """创建带防抖和自动整理功能的 Life 事件回调

    当监控到源目录有新增/转存/移动文件事件时，自动触发媒体整理。
    当监控到目标目录删除事件时，触发 STRM 孤儿清理。

    Args:
        source_dir: 网盘转存源目录路径
        drive_index: 115 账号索引
        target_dir: 媒体库目标目录路径
        source_cid: 网盘转存源目录 cid
        target_cid: 媒体库目标目录 cid
    """
    source_cid_str = str(source_cid or "")
    target_cid_str = str(target_cid or "")

    def callback(
        file_path: str,
        file_id: str,
        event_name: str,
        event_type_cn: str,
        file_cid: str = "",
        file_name: str = "",
        raw_event: dict = None,
    ):
        organize_trigger_events = ("upload_file", "upload_image_file", "receive_files", "move_file", "move_image_file", "copy_folder")
        target_add_events = organize_trigger_events + ("add_folder", "copy_folder")

        current_cid = str(file_cid or "")
        raw = raw_event or {}

        path_in_source = bool(source_dir and (file_path == source_dir or file_path.startswith(source_dir + "/")))
        path_in_target = bool(target_dir and (file_path == target_dir or file_path.startswith(target_dir + "/")))
        cid_in_source = bool(source_cid_str and current_cid and current_cid == source_cid_str)
        cid_in_target = bool(target_cid_str and current_cid and current_cid == target_cid_str)

        is_move_event = event_name in ("move_file", "move_image_file")
        raw_sha1 = str(raw.get("sha1", "") or "").strip()
        is_file_event = bool(raw.get("file_category") == 1 or raw_sha1 or str(raw.get("ico", "") or "").strip())

        library_task_key = build_task_key(drive_index, target_dir)
        current_parent_path = ""

        if is_move_event:
            current_parent_path = _resolve_parent_path_for_move(
                current_cid,
                library_task_key,
                source_dir,
                target_dir,
                source_cid_str,
                target_cid_str,
            )
            current_path_for_move = _join_remote_path(current_parent_path, file_name) if current_parent_path and file_name else ""
            current_in_target = bool(
                current_parent_path and target_dir and (current_parent_path == target_dir or current_parent_path.startswith(target_dir + "/"))
            ) or cid_in_target
            current_in_source = bool(
                current_parent_path and source_dir and (current_parent_path == source_dir or current_parent_path.startswith(source_dir + "/"))
            ) or cid_in_source or (not current_in_target and path_in_source)
        else:
            current_path_for_move = str(file_path or "")
            current_in_source = path_in_source
            current_in_target = path_in_target

        is_source_organize_event = event_name in target_add_events and (
            (current_in_source if is_move_event else (path_in_source or cid_in_source))
        )

        # 过滤整理自身产生的事件（move/rename 事件 file_id 命中记录表且 current_path 匹配绑定目标路径）
        if is_move_event or event_name in ("file_rename", "folder_rename"):
            if _is_self_organized_event(str(file_id or ""), current_path_for_move):
                logger.debug(f"[115Life] 跳过整理自身产生的事件: event={event_name}, path={current_path_for_move}, file_id={file_id}")
                return

        if event_name in ("file_rename", "folder_rename"):
            cached_rename_item = get_task_item_by_id(library_task_key, str(file_id or "")) if str(file_id or "") else None
            if not cached_rename_item:
                return

            cached_rename_path = str(cached_rename_item.get("path", "") or "")
            cached_is_dir = bool(cached_rename_item.get("is_dir"))
            new_remote_path = str(file_path or "") or cached_rename_path
            new_name = str(file_name or os.path.basename(new_remote_path) or cached_rename_item.get("name", "") or "")

            logger.debug(f"[115Life] rename事件通过file_id命中媒体库缓存: file_id={file_id}, file={file_name}")

            try:
                from app.services.strm_service import strm_service
                rename_reason = (
                    f"115目标目录rename事件: {event_type_cn} {cached_rename_path or file_name} "
                    f"-> {new_remote_path or new_name} (event={event_name}, cid={current_cid})"
                )

                if cached_is_dir and cached_rename_path and new_remote_path:
                    updated_count = update_items_path_prefix(
                        library_task_key,
                        cached_rename_path,
                        new_remote_path,
                        meta={"last_status": "updated_by_target_rename_event"},
                    )
                    update_task_item_fields(
                        library_task_key,
                        str(file_id or ""),
                        name=new_name,
                        meta={"last_status": "updated_by_target_rename_event"},
                    )
                    rename_result = strm_service.rename_local_folder_for_remote_subpath(
                        cached_rename_path,
                        new_remote_path,
                        reason=rename_reason,
                    )
                    logger.info(f"[115Life] 目标目录rename事件已同步目录: cache_updated={updated_count} local={rename_result}")
                elif (not cached_is_dir) and cached_rename_path and new_remote_path and _is_video_cache_item(cached_rename_item):
                    update_task_item_fields(
                        library_task_key,
                        str(file_id or ""),
                        name=new_name,
                        path=new_remote_path,
                        meta={"last_status": "updated_by_target_rename_event"},
                    )
                    rename_result = strm_service.rename_local_strm_for_remote_file(
                        cached_rename_path,
                        new_remote_path,
                        reason=rename_reason,
                    )
                    logger.info(f"[115Life] 目标目录rename事件已同步文件: {rename_result}")
            except Exception as e:
                logger.error(f"[115Life] 目标目录rename处理失败: {e}")
            return

        if event_name == "delete_file":
            cached_delete_item = get_task_item_by_id(library_task_key, str(file_id or "")) if str(file_id or "") else None
            if not cached_delete_item:
                return

            logger.debug(f"[115Life] 删除事件通过file_id命中媒体库缓存: file_id={file_id}, file={file_name}")
            cached_delete_path = str(cached_delete_item.get("path", "") or "")
            cached_is_dir = bool(cached_delete_item.get("is_dir"))

            try:
                if cached_is_dir and cached_delete_path:
                    removed_count = remove_items_by_path_prefix(
                        library_task_key,
                        cached_delete_path,
                        meta={"last_status": "updated_by_target_delete_event"},
                    )
                    if removed_count:
                        logger.info(f"[115Life] 已实时从媒体库缓存移除文件夹及子条目 {removed_count} 条: {cached_delete_path}")
                else:
                    removed_count = remove_task_item_by_id(
                        library_task_key,
                        str(file_id or ""),
                        meta={"last_status": "updated_by_target_delete_event"},
                    )
                    if removed_count:
                        logger.info(f"[115Life] 已实时从媒体库缓存移除 {removed_count} 条: file_id={file_id}")
            except Exception as e:
                logger.debug(f"[115Life] 删除事件实时更新媒体库缓存失败: {e}")

            try:
                from app.services.strm_service import strm_service
                cleanup_reason = (
                    f"115目标目录删除事件: {event_type_cn} {cached_delete_path or file_name} "
                    f"(event={event_name}, cid={current_cid})"
                )
                if cached_is_dir and cached_delete_path:
                    cleanup_result = strm_service.remove_local_folder_for_remote_subpath(
                        cached_delete_path,
                        reason=cleanup_reason,
                    )
                else:
                    cleanup_result = strm_service.remove_local_strm_for_remote_file(
                        cached_delete_path,
                        reason=cleanup_reason,
                    )
                logger.info(f"[115Life] 目标目录删除事件触发STRM清理: {cleanup_result}")
            except Exception as e:
                logger.error(f"[115Life] 目标目录删除触发STRM清理失败: {e}")
            return

        raw_sha1 = str(raw.get("sha1", "") or "").upper()
        cached_move_item = get_task_item_by_id(library_task_key, str(file_id or "")) if is_move_event and str(file_id or "") else None
        cached_move_path = str((cached_move_item or {}).get("path", "") or "")
        cached_move_is_dir = bool((cached_move_item or {}).get("is_dir"))
        move_prev_cat = "media_lib" if cached_move_item else "other"
        move_curr_cat = "media_lib" if current_in_target else ("source" if current_in_source else "other")

        is_target_add_event = False
        is_target_delete_event = False
        direction_cn = ""
        action_desc = "无"

        if is_move_event:
            if move_prev_cat == "other" and move_curr_cat == "other":
                return
            if move_prev_cat == "other" and move_curr_cat == "source":
                is_source_organize_event = True
            elif move_prev_cat == "media_lib" and move_curr_cat == "media_lib":
                is_target_delete_event = True
                is_target_add_event = True
            elif move_prev_cat == "media_lib" and move_curr_cat == "source":
                is_target_delete_event = True
                is_source_organize_event = True
            elif move_prev_cat == "media_lib" and move_curr_cat == "other":
                is_target_delete_event = True
            elif move_curr_cat == "media_lib":
                is_target_add_event = True
            direction_cn = f"{move_prev_cat}→{move_curr_cat}"
        else:
            is_target_add_event = path_in_target and (event_name in target_add_events or event_type_cn in ("新建文件夹", "复制文件夹"))
            is_target_delete_event = False
            direction_cn = "inside" if (path_in_target or path_in_source) else ""

        # 整理目录事件：只允许 organize 触发，其余直接跳过
        if (path_in_source or cid_in_source) and not is_source_organize_event:
            return

        if not is_source_organize_event and not is_target_add_event and not is_target_delete_event:
            return

        if is_move_event:
            _cat_cn = {"media_lib": "媒体库", "source": "待整理", "other": "其它"}
            if "→" in direction_cn:
                prev_cat, curr_cat = direction_cn.split("→", 1)
                direction_cn = f"{_cat_cn.get(prev_cat, prev_cat)}→{_cat_cn.get(curr_cat, curr_cat)}"

            _action_map = {
                "media_lib→media_lib": "STRM同步（先删后加）",
                "media_lib→source": "先删再整理",
                "media_lib→other": "STRM清理",
                "other→media_lib": "STRM生成",
                "other→source": "触发整理",
            }
            action_desc = _action_map.get(f"{move_prev_cat}→{move_curr_cat}", "无")
        else:
            parts = []
            if is_target_delete_event:
                parts.append("删除")
            if is_target_add_event:
                parts.append("新增")
            if is_source_organize_event:
                parts.append("整理")
            action_desc = "+".join(parts) if parts else "无"

        logger.info(
            f"[115Life] 事件命中: 方向={direction_cn}, 动作={action_desc}, "
            f"事件={event_type_cn}, 文件={file_name}, 文件ID={file_id}"
        )

        # 目标目录删除事件：先实时删媒体库缓存，再触发 STRM 孤儿清理
        if is_target_delete_event:
            try:
                folder_delete_path = ""
                if (is_move_event and cached_move_is_dir) or (not is_move_event and not is_file_event):
                    folder_delete_path = cached_move_path or file_path
                if folder_delete_path:
                    removed_folder = remove_items_by_path_prefix(
                        library_task_key, folder_delete_path,
                        meta={"last_status": "updated_by_target_delete_event"},
                    )
                    if removed_folder:
                        logger.info(f"[115Life] 已实时从媒体库缓存移除文件夹及子条目 {removed_folder} 条: {folder_delete_path}")
                else:
                    removed_count = remove_task_item_by_id(
                        library_task_key,
                        str(file_id or ""),
                        meta={"last_status": "updated_by_target_delete_event"},
                    )
                    if removed_count:
                        logger.info(f"[115Life] 已实时从媒体库缓存移除 {removed_count} 条: file_id={file_id}")
            except Exception as e:
                logger.debug(f"[115Life] 删除事件实时更新媒体库缓存失败: {e}")

            try:
                from app.services.strm_service import strm_service
                cleanup_reason = (
                    f"115目标目录删除事件: {event_type_cn} {cached_move_path or file_path} "
                    f"(event={event_name}, cid={current_cid}, direction={direction_cn})"
                )
                direct_remove = is_move_event or move_curr_cat in ("source", "other")
                if folder_delete_path:
                    if direct_remove:
                        cleanup_result = strm_service.remove_local_folder_for_remote_subpath(
                            folder_delete_path,
                            reason=cleanup_reason,
                        )
                    else:
                        cleanup_result = strm_service.cleanup_orphan_for_remote_subpath(
                            folder_delete_path,
                            reason=cleanup_reason,
                        )
                        if cleanup_result.get("status") == "error":
                            cleanup_result = strm_service.cleanup_orphan_for_remote_path(
                                target_dir,
                                reason=f"{cleanup_reason} | fallback=target_dir",
                            )
                else:
                    if direct_remove and cached_move_path:
                        cleanup_result = strm_service.remove_local_strm_for_remote_file(
                            cached_move_path,
                            reason=cleanup_reason,
                        )
                    else:
                        cleanup_result = strm_service.cleanup_orphan_for_remote_path(
                            target_dir,
                            reason=cleanup_reason,
                        )
                logger.info(f"[115Life] 目标目录删除事件触发STRM清理: {cleanup_result}")
            except Exception as e:
                logger.error(f"[115Life] 目标目录删除触发STRM清理失败: {e}")
            if not is_target_add_event and not is_source_organize_event:
                return

        # 目标目录新增事件：source 下直接触发整理；target 下走 cache-first 同步
        if is_target_add_event:
            if path_in_source or current_in_source:
                is_source_organize_event = True
            elif path_in_target or current_in_target:
                target_event_path = str(file_path or "")
                if not target_event_path and is_move_event and file_name:
                    if current_parent_path:
                        target_event_path = _join_remote_path(current_parent_path, file_name)
                    elif cid_in_target:
                        target_event_path = _join_remote_path(target_dir, file_name)
                target_parent_path = ""
                if is_file_event:
                    target_parent_path = current_parent_path or _remote_dirname(target_event_path)
                _schedule_or_refresh_target_event_poll(
                    target_event_path,
                    file_id,
                    drive_index,
                    target_dir,
                    event_name,
                    event_type_cn,
                    is_file_event=is_file_event,
                    parent_cid=current_cid if is_file_event else "",
                    parent_path=target_parent_path,
                )
                return

        _schedule_or_refresh_source_poll(
            drive_index=drive_index,
            source_dir=source_dir,
            target_dir=target_dir,
            source_cid=source_cid_str,
        )

    return callback


# ---------------------------------------------------------------------------
# _collect_target_event_entries
# ---------------------------------------------------------------------------

async def _collect_target_event_entries(
    file_path: str,
    file_id: str,
    drive_index: int,
    target_dir: str,
    *,
    include_status: bool = False,
) -> List[dict] | tuple[List[dict], bool]:
    unstable = False

    def _done(items: List[dict]):
        return (items, unstable) if include_status else items

    remote_file_path = (file_path or "").rstrip("/")
    td = (target_dir or "").rstrip("/")
    if not td:
        return _done([])

    entries: List[dict] = []
    if not file_id or not str(file_id).isdigit():
        if not remote_file_path or not (remote_file_path == td or remote_file_path.startswith(td + "/")):
            return _done([])
        normalized = _normalize_target_entry({
            "id": file_id,
            "name": os.path.basename(remote_file_path),
            "path": remote_file_path,
            "is_dir": False,
        })
        return _done([normalized] if normalized else [])

    try:
        import warnings
        from app.services.drive115_service import drive115_service
        from p115client.tool.iterdir import iter_files_with_path
        from p115client.tool.attr import get_attr

        client, _ = await drive115_service.get_client(drive_index)
        if not client:
            return _done([])

        with _read_lock:
            attr = get_attr(client, int(file_id)) or {}
        is_dir = bool(
            attr.get("is_dir")
            or attr.get("is_directory")
            or str(attr.get("fc", "")) == "0"
        )

        if not is_dir:
            path = str(attr.get("path") or remote_file_path).rstrip("/")
            if not path or not (path == td or path.startswith(td + "/")):
                return _done([])
            normalized = _normalize_target_entry({
                "id": int(file_id),
                "name": str(attr.get("name") or os.path.basename(path) or ""),
                "path": path,
                "pickcode": str(attr.get("pickcode") or attr.get("pick_code") or attr.get("pc") or ""),
                "size": int(attr.get("size", 0) or 0),
                "sha1": str(attr.get("sha1", "") or ""),
                "is_dir": False,
                "parent_id": int(attr.get("parent_id", attr.get("cid", 0)) or 0),
            })
            return _done([normalized] if normalized else [])

        folder_path = str(attr.get("path") or remote_file_path).rstrip("/")
        if not folder_path or not (folder_path == td or folder_path.startswith(td + "/")):
            return _done([])
        folder_entry = _normalize_target_entry({
            "id": int(file_id),
            "name": str(attr.get("name") or os.path.basename(folder_path) or ""),
            "path": folder_path,
            "pickcode": str(attr.get("pickcode") or attr.get("pick_code") or attr.get("pc") or ""),
            "size": int(attr.get("size", 0) or 0),
            "sha1": str(attr.get("sha1", "") or ""),
            "is_dir": True,
            "parent_id": int(attr.get("parent_id", attr.get("cid", 0)) or 0),
        })
        if folder_entry:
            entries.append(folder_entry)

        caught_warnings = []
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with _read_lock:
                scanned_items = list(iter_files_with_path(client, cid=int(file_id), app="android", cooldown=1.0, page_size=1000))
            caught_warnings = list(caught)
        for warning_item in caught_warnings:
            message = str(getattr(warning_item, "message", "") or "")
            if "detected count changes during iteration" in message:
                unstable = True
                logger.info(f"[115Life] 目标新增事件扫描检测到目录数量变化，等待稳定: {message}")
            else:
                warnings.warn(warning_item.message, warning_item.category)
        for item in scanned_items:
            normalized = _normalize_target_entry(item)
            if not normalized:
                continue
            path = str(normalized.get("path", "") or "").rstrip("/")
            if not path or not (path == td or path.startswith(td + "/")):
                continue
            entries.append(normalized)
    except Exception as e:
        if "detected count changes during iteration" in str(e):
            unstable = True
        logger.warning(
            f"[115Life] 目标目录新增事件递归扫描失败: type={type(e).__name__} "
            f"repr={e!r} file_id={file_id} file_path={file_path} target_dir={target_dir}",
            exc_info=True,
        )

    return _done(entries)


def _build_target_event_session_key(drive_index: int, target_dir: str, scan_cid: str, scan_path: str) -> str:
    scope = str(scan_cid or "").strip() or _normalize_remote_path(scan_path)
    return f"{int(drive_index)}:{_normalize_remote_path(target_dir)}:{scope}"


def _target_event_entries_signature(entries: List[dict]) -> str:
    hasher = hashlib.sha1()
    for entry in sorted(entries or [], key=lambda x: str(x.get("item_key", "") or x.get("path", ""))):
        hasher.update(
            (
                f"{entry.get('item_key', '')}|{int(entry.get('size', 0) or 0)}|"
                f"{entry.get('path', '')}|{entry.get('sha1', '')}|{1 if entry.get('is_dir') else 0}\n"
            ).encode("utf-8", errors="ignore")
        )
    return hasher.hexdigest()


def _target_cache_item_changed(cached: dict | None, item: dict) -> bool:
    if not cached:
        return True
    for key in ("name", "path", "pickcode", "sha1"):
        if str(cached.get(key, "") or "") != str(item.get(key, "") or ""):
            return True
    for key in ("size", "id", "parent_id"):
        try:
            if int(cached.get(key, 0) or 0) != int(item.get(key, 0) or 0):
                return True
        except (TypeError, ValueError):
            return True
    return bool(cached.get("is_dir", False)) != bool(item.get("is_dir", False))


def _schedule_or_refresh_target_event_poll(
    file_path: str,
    file_id: str,
    drive_index: int,
    target_dir: str,
    event_name: str,
    event_type_cn: str,
    *,
    is_file_event: bool = False,
    parent_cid: str = "",
    parent_path: str = "",
):
    scan_cid = str(parent_cid or "").strip() if is_file_event else str(file_id or "").strip()
    scan_path = _normalize_remote_path(parent_path) if is_file_event and scan_cid else _normalize_remote_path(file_path)
    if is_file_event and not scan_cid:
        scan_cid = str(file_id or "").strip()
        scan_path = _normalize_remote_path(file_path)
    if not scan_path and target_dir:
        scan_path = _normalize_remote_path(target_dir)

    if not scan_cid and not scan_path:
        logger.warning(f"[115Life] 目标新增事件缺少可轮询范围: event={event_name}, file_id={file_id}, path={file_path}")
        return
    if not _state._main_event_loop or not _state._main_event_loop.is_running():
        logger.error("[115Life] 主事件循环未注册，无法处理目标新增事件")
        return

    session_key = _build_target_event_session_key(drive_index, target_dir, scan_cid, scan_path)
    now = _time.time()
    with _target_event_lock:
        session = _state._target_event_sessions.get(session_key)
        if session is None:
            session = {
                "session_key": session_key,
                "drive_index": int(drive_index),
                "target_dir": _normalize_remote_path(target_dir),
                "scan_cid": scan_cid,
                "scan_path": scan_path,
                "event_name": event_name,
                "event_type_cn": event_type_cn,
                "last_scan_signature": "",
                "unchanged_polls": 0,
                "polls": 0,
                "event_generation": 1,
                "event_count": 1,
                "started_at": now,
                "max_polls": 60,
            }
            _state._target_event_sessions[session_key] = session
            logger.info(f"[115Life] 已创建目标新增稳定轮询会话: key={session_key} scope={scan_path or scan_cid}")
        else:
            session["event_name"] = event_name
            session["event_type_cn"] = event_type_cn
            session["event_generation"] = int(session.get("event_generation", 0) or 0) + 1
            session["event_count"] = int(session.get("event_count", 0) or 0) + 1
            session["last_scan_signature"] = ""
            session["unchanged_polls"] = 0
            logger.info(
                f"[115Life] 已刷新目标新增稳定轮询会话: key={session_key} "
                f"generation={session['event_generation']} events={session['event_count']}"
            )

        should_schedule = not _state._target_event_running
        if should_schedule:
            _state._target_event_running = True

    if should_schedule:
        try:
            asyncio.run_coroutine_threadsafe(_run_target_event_poll_loop(), _state._main_event_loop)
        except Exception as e:
            with _target_event_lock:
                _state._target_event_running = False
            logger.error(f"[115Life] 目标新增稳定轮询调度失败: {e}")


async def _process_target_event_entries(
    entries: List[dict],
    drive_index: int,
    target_dir: str,
    event_name: str,
    event_type_cn: str,
    scan_path: str,
):
    try:
        if not entries:
            logger.info(f"[115Life] 目标新增事件稳定后无可同步条目: {scan_path}")
            return

        deduped = {}
        for entry in entries:
            item_key = str(entry.get("item_key", "") or "")
            if item_key:
                deduped[item_key] = entry
        entries = list(deduped.values())

        task_key = build_task_key(drive_index, target_dir)
        existing_items = get_task_items(task_key)
        cache_items = {
            entry["item_key"]: entry["cache_item"]
            for entry in entries
            if entry.get("item_key") and entry.get("cache_item")
        }
        if cache_items:
            try:
                merge_task_items(
                    task_key,
                    cache_items,
                    meta={"last_status": "updated_by_target_add_event"},
                )
                logger.info(f"[115Life] 目标新增事件已批量写入缓存 {len(cache_items)} 条: {scan_path}")
            except Exception as e:
                logger.debug(f"[115Life] 目标新增事件批量写缓存失败: {e}")

        from app.services.strm_service import strm_service

        dir_entries = []
        changed_file_entries = []
        unchanged_files = 0
        for entry in entries:
            remote_file_path = str(entry.get("path", "") or "").rstrip("/")
            if entry.get("is_dir"):
                dir_entries.append(entry)
                continue
            if not remote_file_path or _is_recent_organize_generated(remote_file_path):
                continue
            item_key = str(entry.get("item_key", "") or "")
            cache_item = entry.get("cache_item") or {}
            if _target_cache_item_changed(existing_items.get(item_key), cache_item):
                changed_file_entries.append(entry)
            else:
                unchanged_files += 1

        dir_count = 0
        dir_failed = 0
        for entry in dir_entries:
            remote_dir_path = str(entry.get("path", "") or "").rstrip("/")
            if not remote_dir_path:
                continue
            result = strm_service.ensure_local_dir_for_remote_path(
                remote_dir_path,
                reason=f"115目标目录新增事件: {event_type_cn} {remote_dir_path} (event={event_name})",
            )
            if result.get("status") == "ok":
                dir_count += 1
            elif result.get("status") == "error":
                dir_failed += 1

        missing_pickcode_entries = [
            entry for entry in changed_file_entries
            if not str(entry.get("pickcode", "") or "") and str(entry.get("file_id", "") or "").isdigit()
        ]
        if missing_pickcode_entries:
            try:
                from app.services.drive115_service import drive115_service
                from p115client.tool.attr import get_attr
                client, _ = await drive115_service.get_client(drive_index)
                if client:
                    for entry in missing_pickcode_entries:
                        try:
                            with _read_lock:
                                attr = get_attr(client, int(entry["file_id"])) or {}
                            pickcode = str(attr.get("pickcode") or attr.get("pick_code") or attr.get("pc") or "")
                            if not pickcode:
                                continue
                            entry["pickcode"] = pickcode
                            cache_item = dict(entry.get("cache_item") or {})
                            cache_item["pickcode"] = pickcode
                            entry["cache_item"] = cache_item
                            upsert_task_item(
                                task_key,
                                entry["file_id"],
                                cache_item,
                                meta={"last_status": "updated_by_target_add_event"},
                            )
                        except Exception:
                            continue
            except Exception as e:
                logger.debug(f"[115Life] 目标新增事件补取 pickcode 失败: {e}")

        incremental_items = []
        for entry in changed_file_entries:
            cache_item = dict(entry.get("cache_item") or {})
            if not cache_item.get("pickcode"):
                continue
            incremental_items.append(cache_item)

        sync_result = strm_service.process_incremental_items(incremental_items) if incremental_items else {
            "generated": 0,
            "downloaded": 0,
            "download_failed": 0,
            "failed": 0,
            "skipped": 0,
            "matched_items": 0,
        }

        logger.info(
            f"[115Life] 目标目录新增事件处理完成: 事件={event_name}/{event_type_cn} 范围={scan_path} "
            f"扫描={len(entries)} 缓存={len(cache_items)} 目录创建={dir_count} 目录失败={dir_failed} "
            f"待同步={len(incremental_items)} strm={int(sync_result.get('generated', 0) or 0)} "
            f"附属下载={int(sync_result.get('downloaded', 0) or 0)} "
            f"失败={int(sync_result.get('failed', 0) or 0) + int(sync_result.get('download_failed', 0) or 0)} "
            f"跳过={int(sync_result.get('skipped', 0) or 0) + unchanged_files}"
        )
    except Exception as e:
        logger.error(f"[115Life] 处理目标目录新增事件失败: {e}", exc_info=True)


async def _run_target_event_poll_loop():
    try:
        while True:
            with _target_event_lock:
                sessions = [dict(s) for s in _state._target_event_sessions.values()]
            if not sessions:
                with _target_event_lock:
                    _state._target_event_running = False
                return

            for session in sessions:
                session_key = str(session.get("session_key", "") or "")
                if not session_key:
                    continue
                current = _state._target_event_sessions.get(session_key)
                if not current:
                    continue

                drive_index = int(current.get("drive_index", 0) or 0)
                target_dir = str(current.get("target_dir", "") or "")
                scan_cid = str(current.get("scan_cid", "") or "")
                scan_path = str(current.get("scan_path", "") or "")
                event_name = str(current.get("event_name", "") or "")
                event_type_cn = str(current.get("event_type_cn", "") or "")
                generation = int(current.get("event_generation", 0) or 0)

                entries, unstable = await _collect_target_event_entries(
                    scan_path,
                    scan_cid,
                    drive_index,
                    target_dir,
                    include_status=True,
                )
                signature = _target_event_entries_signature(entries)
                last_signature = str(current.get("last_scan_signature", "") or "")
                unchanged_polls = int(current.get("unchanged_polls", 0) or 0)
                polls = int(current.get("polls", 0) or 0) + 1
                max_polls = int(current.get("max_polls", 60) or 60)
                file_count = sum(1 for entry in entries if not entry.get("is_dir"))
                dir_count = sum(1 for entry in entries if entry.get("is_dir"))

                if unstable:
                    unchanged_polls = 0
                    signature_for_next = ""
                elif last_signature and signature == last_signature:
                    unchanged_polls += 1
                    signature_for_next = signature
                else:
                    unchanged_polls = 0
                    signature_for_next = signature

                current["last_scan_signature"] = signature_for_next
                current["unchanged_polls"] = unchanged_polls
                current["polls"] = polls

                logger.info(
                    f"[115Life] 目标新增稳定轮询: key={session_key} 文件={file_count} 目录={dir_count} "
                    f"签名={signature[:8]} 不变={unchanged_polls} unstable={unstable} polls={polls}"
                )

                stable = bool(entries) and not unstable and unchanged_polls >= 1
                timeout = polls >= max_polls
                if not stable and not timeout:
                    continue

                if timeout and not stable:
                    logger.warning(
                        f"[115Life] 目标新增稳定轮询达到上限，按当前快照同步: key={session_key} 文件={file_count} 目录={dir_count}"
                    )
                else:
                    logger.info(f"[115Life] 目标新增稳定窗口命中，开始批量同步: key={session_key} 文件={file_count} 目录={dir_count}")

                await _process_target_event_entries(entries, drive_index, target_dir, event_name, event_type_cn, scan_path)

                with _target_event_lock:
                    latest = _state._target_event_sessions.get(session_key)
                    if latest and int(latest.get("event_generation", 0) or 0) == generation:
                        _state._target_event_sessions.pop(session_key, None)
                    elif latest:
                        latest["last_scan_signature"] = ""
                        latest["unchanged_polls"] = 0

            await asyncio.sleep(5)
    except Exception as e:
        with _target_event_lock:
            _state._target_event_running = False
        logger.error(f"[115Life] 目标新增稳定轮询异常退出: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# _sync_target_event_to_local
# ---------------------------------------------------------------------------

async def _sync_target_event_to_local(
    file_path: str,
    file_id: str,
    drive_index: int,
    target_dir: str,
    event_name: str,
    event_type_cn: str,
):
    entries = await _collect_target_event_entries(file_path, file_id, drive_index, target_dir)
    logger.info(f"[115Life] 目标新增事件扫描到 {len(entries)} 个条目: {file_path}")
    await _process_target_event_entries(entries, drive_index, target_dir, event_name, event_type_cn, file_path)


# ---------------------------------------------------------------------------
# _drain_target_event_queue
# ---------------------------------------------------------------------------

async def _drain_target_event_queue():
    """串行消费目标目录新增事件队列"""
    while True:
        with _target_event_lock:
            if not _target_event_queue:
                _state._target_event_running = False
                return
            event = _target_event_queue.pop(0)
        await _sync_target_event_to_local(*event)


# ---------------------------------------------------------------------------
# _cleanup_empty_source_dirs
# ---------------------------------------------------------------------------


def _normalize_source_cleanup_relpath(path: str) -> str:
    value = str(path or "").strip().strip("/")
    if value in {"", "."}:
        return ""
    return value


def _get_source_cleanup_parent_path(path: str) -> str:
    normalized = _normalize_source_cleanup_relpath(path)
    if not normalized or "/" not in normalized:
        return ""
    return normalized.rsplit("/", 1)[0]


def _build_source_cleanup_delete_queue(client, source_cid: int) -> list[tuple[str, str, bool]]:
    from p115client.tool.iterdir import iter_dirs_with_path, iter_files_with_path

    root_path = ""
    dir_id_by_path: dict[str, int] = {root_path: int(source_cid)}
    parent_by_path: dict[str, str] = {}
    children_by_parent: dict[str, set[str]] = {root_path: set()}
    direct_media_dirs: set[str] = set()
    root_non_media_files: list[tuple[str, str, bool]] = []
    scanned_dir_count = 0
    scanned_file_count = 0

    for item in iter_dirs_with_path(client, cid=int(source_cid), app="android"):
        relpath = _normalize_source_cleanup_relpath(item.get("relpath") or item.get("path") or "")
        if not relpath:
            continue
        dir_id = int(item.get("id") or 0)
        if not dir_id:
            continue
        parent_path = _get_source_cleanup_parent_path(relpath)
        dir_id_by_path[relpath] = dir_id
        parent_by_path[relpath] = parent_path
        children_by_parent.setdefault(parent_path, set()).add(relpath)
        children_by_parent.setdefault(relpath, set())
        scanned_dir_count += 1

    with _read_lock:
        scanned_files = list(iter_files_with_path(client, cid=int(source_cid), app="android", cooldown=1.0, page_size=1000))

    for item in scanned_files:
        relpath = _normalize_source_cleanup_relpath(item.get("relpath") or item.get("path") or "")
        if not relpath:
            continue
        scanned_file_count += 1
        file_name = str(item.get("name", "") or "")
        file_id = str(item.get("id") or item.get("fid") or "")
        parent_path = _get_source_cleanup_parent_path(relpath)
        ext = os.path.splitext(file_name)[1].lower()
        if ext in VIDEO_EXTS or ext in SUBTITLE_EXTS:
            direct_media_dirs.add(parent_path)
        elif not parent_path and file_id:
            root_non_media_files.append((file_id, file_name, False))

    logger.info(f"[MediaOrganize] 清理索引完成: 目录 {scanned_dir_count} 个，文件 {scanned_file_count} 个")

    has_media_subtree: dict[str, bool] = {path: False for path in dir_id_by_path}
    for path in sorted(dir_id_by_path.keys(), key=lambda value: value.count("/"), reverse=True):
        has_media = path in direct_media_dirs
        if not has_media:
            has_media = any(has_media_subtree.get(child, False) for child in children_by_parent.get(path, set()))
        has_media_subtree[path] = has_media

    root_has_media = has_media_subtree.get(root_path, False)
    to_delete: list[tuple[str, str, bool]] = []

    for path in sorted((p for p in dir_id_by_path.keys() if p), key=lambda value: value.count("/")):
        parent_path = parent_by_path.get(path, "")
        if has_media_subtree.get(path, False):
            continue
        if parent_path and not has_media_subtree.get(parent_path, False):
            continue
        to_delete.append((str(dir_id_by_path[path]), path, True))

    if not root_has_media:
        to_delete.extend(root_non_media_files)

    return to_delete


async def _cleanup_empty_source_dirs(client, source_cid: str):
    """清理源目录下不包含视频或字幕的子目录，并按旧语义处理根目录下的非媒体文件。"""
    try:
        to_delete = _build_source_cleanup_delete_queue(client, int(source_cid))

        files_count = sum(1 for _, _, is_dir in to_delete if not is_dir)
        dirs_count = sum(1 for _, _, is_dir in to_delete if is_dir)
        logger.info(f"[MediaOrganize] 清理扫描完成: 待删文件 {files_count} 个，目录 {dirs_count} 个")
        if not to_delete:
            return

        # 按路径深度排序（深的先删，同深度文件先于目录）
        to_delete.sort(key=lambda x: (x[1].count("/"), not x[2]), reverse=True)

        deleted_files = 0
        deleted_dirs = 0
        def _is_delete_pending_error(err) -> bool:
            err_str = str(err)
            return "操作尚未执行完成" in err_str or "990009" in err_str

        async def _delete_ids_with_retry(ids: list[int], log_label: str):
            retry_delays = (0.0, 1.0, 2.0, 3.0)
            last_error = None
            for attempt, delay in enumerate(retry_delays, start=1):
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    resp = _run_115_write_request_sync(
                        client,
                        f"清理源目录{log_label}",
                        lambda write_client: write_client.fs_delete(ids, async_=False),
                        raise_on_state_false=False,
                    )
                    if isinstance(resp, dict) and resp.get("state") is False:
                        raise RuntimeError(resp)
                    if attempt > 1:
                        logger.info(f"[MediaOrganize] 删除重试成功: {log_label} attempt={attempt}")
                    return True, None
                except Exception as e:
                    last_error = e
                    if not _is_delete_pending_error(e) or attempt == len(retry_delays):
                        return False, e
                    logger.info(f"[MediaOrganize] 删除任务仍在执行，等待后重试: {log_label} attempt={attempt}")
            return False, last_error

        async def _flush_batch(items: list[tuple[str, str, bool]]):
            nonlocal deleted_files, deleted_dirs
            if not items:
                return

            batch_ids = [int(did) for did, _, _ in items]
            ok, error = await _delete_ids_with_retry(batch_ids, f"batch size={len(items)}")
            if ok:
                deleted_dirs += sum(1 for _, _, is_dir in items if is_dir)
                deleted_files += sum(1 for _, _, is_dir in items if not is_dir)
                logger.info(f"[MediaOrganize] 批量删除成功: {len(items)} 项")
            else:
                logger.warning(f"[MediaOrganize] 批量删除失败，回退逐条处理: {error}")
                for did, path, is_dir in items:
                    label = "目录" if is_dir else "文件"
                    ok, inner_error = await _delete_ids_with_retry([int(did)], f"{label}:{path}")
                    if ok:
                        if is_dir:
                            deleted_dirs += 1
                        else:
                            deleted_files += 1
                        logger.debug(f"[MediaOrganize] 删除{label}: {path}")
                        continue

                    err_str = str(inner_error)
                    if "目录不存在" in err_str or "文件不存在" in err_str:
                        if is_dir:
                            deleted_dirs += 1
                        else:
                            deleted_files += 1
                        logger.debug(f"[MediaOrganize] 删除{label}: {path}")
                    else:
                        logger.warning(f"[MediaOrganize] 删除{label}失败 {path}: {inner_error}")
            await asyncio.sleep(1)

        await _flush_batch(to_delete)

        parts = []
        if deleted_files:
            parts.append(f"{deleted_files} 个文件")
        if deleted_dirs:
            parts.append(f"{deleted_dirs} 个目录")
        if parts:
            logger.info(f"[MediaOrganize] 清理完成，共删除 {'、'.join(parts)}")
    except Exception as e:
        logger.error(f"[MediaOrganize] 清理空文件夹失败: {e}", exc_info=True)
