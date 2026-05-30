"""Emby 媒体库状态协调：启动加载、持久化状态、按需建库"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from core.configs import EMBY_DISCOVER_INDEX_FILE
from core.cache_db import cache_db
from core.emby_client import EmbyClient
from app.routers.config_302 import get_emby_config_by_index_sync
from app.services.media_organize_state import CONFIG_FILE as MEDIA_ORGANIZE_CONFIG_FILE
from app.services.realtime_events import publish_realtime_event

logger = logging.getLogger("EmbyLibCache")

STATE_FILE = "config/media_organize_emby_state.json"
STRM_CONFIG_FILE = "config/strm_config.json"

# 内存缓存：{(server_idx, media_type, level_key): True}
_cache: dict[tuple[int, str, str], bool] = {}
_lock = threading.RLock()
_enabled = False
_server_idx = 0
_level = "level1"

# Emby 可用性索引：整部媒体、标题映射、剧集季/集状态
DISCOVER_INDEX_VERSION = 3
DISCOVER_INDEX_MIN_REFRESH_INTERVAL = 5 * 60


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except Exception:
        value = default
    return min(max(value, minimum), maximum)


DISCOVER_INDEX_LIBRARY_SCAN_WORKERS = _env_int("CHILLPOSTER_DISCOVER_INDEX_LIBRARY_SCAN_WORKERS", 6, 1, 12)
_discover_index: dict[str, str] = {}
_discover_series_index: dict[str, dict[int, set[int]]] = {}
_discover_items: dict[str, dict] = {}
_discover_index_meta: dict = {}
_discover_index_lock = threading.RLock()
_discover_index_built = False
_discover_index_building = False
_discover_index_refresh_pending = False
_discover_index_timer: threading.Timer | None = None
_discover_index_last_finished_at = 0.0
_discover_index_pending_reason = ""
_discover_cache_db_lock = threading.RLock()
_discover_cache_db_ready = False


def _now_ts() -> int:
    return int(time.time())


def normalize_category_path(path: str | None) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    parts = [part.strip() for part in raw.split("/") if part.strip()]
    return "/".join(parts)


def _normalize_fs_path(path: str | None) -> str:
    if not path:
        return ""
    return os.path.normpath(str(path)).rstrip("/\\")


def normalize_library_path(path: str | None) -> str:
    return _normalize_fs_path(path).replace("\\", "/").rstrip("/").lower()


def _default_state() -> dict:
    return {
        "settings_snapshot": {
            "sync_emby_library": True,
            "emby_server_idx": 0,
            "emby_library_level": "level3",
        },
        "desired_libraries": {},
        "local_paths": {},
        "emby_libraries": {},
    }


def _read_json_file(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _atomic_write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_state() -> dict:
    state = _read_json_file(STATE_FILE, _default_state())
    if not isinstance(state, dict):
        state = _default_state()
    state.setdefault("settings_snapshot", {})
    state.setdefault("desired_libraries", {})
    state.setdefault("local_paths", {})
    state.setdefault("emby_libraries", {})
    if not isinstance(state["settings_snapshot"], dict):
        state["settings_snapshot"] = _default_state()["settings_snapshot"]
    if not isinstance(state["desired_libraries"], dict):
        state["desired_libraries"] = {}
    if not isinstance(state["local_paths"], dict):
        state["local_paths"] = {}
    if not isinstance(state["emby_libraries"], dict):
        state["emby_libraries"] = {}
    return state


def save_state(state: dict):
    _atomic_write_json(STATE_FILE, state)


def _desired_key(media_type: str, category_path: str) -> str:
    return f"{media_type}:{category_path}"


def _cache_key(server_idx: int, media_type: str, level_key: str) -> tuple[int, str, str]:
    return server_idx, media_type, normalize_category_path(level_key)


def _extract_settings_snapshot(sc: Optional[dict]) -> dict:
    sc = sc or {}
    return {
        "sync_emby_library": bool(sc.get("sync_emby_library", True)),
        "emby_server_idx": int(sc.get("emby_server_idx", 0) or 0),
        "emby_library_level": sc.get("emby_library_level", "level3") or "level3",
    }


def _emby_scrapers_enabled() -> bool:
    config = _read_json_file(MEDIA_ORGANIZE_CONFIG_FILE, {})
    if not isinstance(config, dict):
        return False
    return bool(config.get("emby_scrapers_enabled", False))


def _build_desired_libraries(rules: dict) -> dict:
    sc = _extract_settings_snapshot((rules or {}).get("sub_classify") or {})
    level = sc["emby_library_level"]
    desired = {}

    for media_type in ("movie", "tv"):
        for rule in (rules or {}).get(media_type, []) or []:
            if not isinstance(rule, dict):
                continue
            category_path = normalize_category_path(rule.get("path"))
            if not category_path:
                continue
            level_key = _truncate_to_level(category_path, level)
            library_name = level_key.rsplit("/", 1)[-1] if level_key else category_path.rsplit("/", 1)[-1]
            desired[_desired_key(media_type, category_path)] = {
                "media_type": media_type,
                "category_path": category_path,
                "level_key": level_key or category_path,
                "library_name": library_name or category_path,
                "desired": True,
            }
    return desired


def diff_rule_paths(old_rules: dict, new_rules: dict) -> dict:
    def _collect_paths(rules: dict) -> set[str]:
        paths: set[str] = set()
        for media_type in ("movie", "tv"):
            for rule in (rules or {}).get(media_type, []) or []:
                if not isinstance(rule, dict):
                    continue
                path = normalize_category_path(rule.get("path"))
                if path:
                    paths.add(path)
        return paths

    old_paths = _collect_paths(old_rules)
    new_paths = _collect_paths(new_rules)
    return {
        "added_paths": sorted(new_paths - old_paths),
        "removed_paths": sorted(old_paths - new_paths),
        "unchanged_paths": sorted(old_paths & new_paths),
    }


def sync_desired_state(rules: Optional[dict] = None) -> dict:
    from app.services.category_matcher import load_rules

    rules = rules if rules is not None else load_rules()
    desired = _build_desired_libraries(rules)
    settings_snapshot = _extract_settings_snapshot((rules or {}).get("sub_classify") or {})

    with _lock:
        state = load_state()
        state["settings_snapshot"] = settings_snapshot
        state["desired_libraries"] = desired

        for key, local_info in state.get("local_paths", {}).items():
            if isinstance(local_info, dict):
                local_info["desired"] = key in desired

        save_state(state)

    return {
        "desired_count": len(desired),
        "settings_snapshot": settings_snapshot,
    }


def _get_server_config(server_idx: int) -> Optional[dict]:
    server = get_emby_config_by_index_sync(server_idx)
    if not isinstance(server, dict):
        return None
    if not server.get("enabled", True):
        return None
    return server


def _create_client(server_idx: int) -> Optional[EmbyClient]:
    server = _get_server_config(server_idx)
    if not server:
        return None
    return EmbyClient(server["url"], server["key"], server.get("public_host"))


def _find_library_by_name_or_path(libs: list[dict], library_name: str, local_path: str = "") -> Optional[dict]:
    norm_local_path = _normalize_fs_path(local_path)

    if norm_local_path:
        for lib in libs:
            lib_paths = [_normalize_fs_path(path) for path in (lib.get("paths") or [])]
            if norm_local_path and norm_local_path in lib_paths:
                return lib

    for lib in libs:
        if lib.get("name") == library_name:
            return lib
    return None


def _collect_server_libraries(client: Optional[EmbyClient]) -> list[dict]:
    if not client:
        return []
    try:
        return client.get_libraries() or []
    except Exception as e:
        logger.warning(f"[EmbyLibCache] 读取 Emby 媒体库失败: {e}")
        return []


def _snapshot_lib_from_live(server_idx: int, lib: dict, state: dict, now: Optional[int] = None) -> dict:
    now = now or _now_ts()
    desired = state.get("desired_libraries", {})
    lib_paths = lib.get("paths") or []
    matched_desired_keys = []
    normalized_lib_paths = {normalize_library_path(path) for path in lib_paths}
    for desired_key, entry in desired.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("library_name") == lib.get("name"):
            matched_desired_keys.append(desired_key)
            continue
        local_info = state.get("local_paths", {}).get(desired_key, {})
        local_path = normalize_library_path(local_info.get("actual_local_path")) if isinstance(local_info, dict) else ""
        if local_path and local_path in normalized_lib_paths:
            matched_desired_keys.append(desired_key)

    return {
        "server_idx": server_idx,
        "library_name": lib.get("name", ""),
        "emby_library_id": lib.get("id"),
        "type": lib.get("type", "unknown"),
        "paths": lib_paths,
        "path_count": len(lib_paths),
        "exists_in_emby": True,
        "matched_desired_keys": sorted(set(matched_desired_keys)),
        "last_synced_at": now,
    }


def _refresh_emby_snapshot_locked(state: dict, server_idx: Optional[int] = None) -> int:
    current_server_idx = server_idx if server_idx is not None else state.get("settings_snapshot", {}).get("emby_server_idx", _server_idx)
    client = _create_client(current_server_idx)
    libs = _collect_server_libraries(client)
    now = _now_ts()

    preserved = {}
    for key, value in state.get("emby_libraries", {}).items():
        if not isinstance(value, dict):
            continue
        if value.get("server_idx") != current_server_idx:
            preserved[key] = value

    for lib in libs:
        lib_id = lib.get("id") or lib.get("name") or f"unknown-{len(preserved)}"
        preserved[f"{current_server_idx}:{lib_id}"] = _snapshot_lib_from_live(
            current_server_idx,
            lib,
            state,
            now=now,
        )

    state["emby_libraries"] = preserved
    if current_server_idx == state.get("settings_snapshot", {}).get("emby_server_idx", _server_idx):
        _reconcile_desired_with_snapshot_locked(state, libs)
    return len(libs)


def _snapshot_libraries_from_state_locked(state: dict, server_idx: int) -> list[dict]:
    libs = []
    for value in state.get("emby_libraries", {}).values():
        if not isinstance(value, dict):
            continue
        if value.get("server_idx") != server_idx:
            continue
        libs.append({
            "name": value.get("library_name", ""),
            "id": value.get("emby_library_id"),
            "type": value.get("type", "unknown"),
            "paths": value.get("paths") or [],
        })
    return libs


def get_server_libraries(server_idx: int, allow_stale: bool = True) -> list[dict]:
    with _lock:
        state = load_state()
        libs = _snapshot_libraries_from_state_locked(state, server_idx)
    if libs or allow_stale:
        return libs
    return refresh_server_libraries(server_idx)


def refresh_server_libraries(server_idx: int) -> list[dict]:
    with _lock:
        state = load_state()
        _refresh_emby_snapshot_locked(state, server_idx=server_idx)
        save_state(state)
        return _snapshot_libraries_from_state_locked(state, server_idx)


def find_libraries_for_path(server_idx: int, target_path: str) -> list[dict]:
    norm_target_path = normalize_library_path(target_path)
    if not norm_target_path:
        return []

    matches = []
    for lib in get_server_libraries(server_idx):
        lib_id = lib.get("id")
        if not lib_id:
            continue
        for lib_path in lib.get("paths", []) or []:
            lib_path_norm = normalize_library_path(lib_path)
            if not lib_path_norm:
                continue
            if norm_target_path.startswith(lib_path_norm) or lib_path_norm.startswith(norm_target_path):
                matches.append(lib)
                break
    return matches


def _reconcile_desired_with_snapshot_locked(state: dict, libs: Optional[list[dict]] = None):
    current_server_idx = state.get("settings_snapshot", {}).get("emby_server_idx", _server_idx)
    if libs is None:
        libs = []
        for value in state.get("emby_libraries", {}).values():
            if not isinstance(value, dict):
                continue
            if value.get("server_idx") != current_server_idx:
                continue
            libs.append({
                "name": value.get("library_name", ""),
                "id": value.get("emby_library_id"),
                "paths": value.get("paths") or [],
                "type": value.get("type", "unknown"),
            })

    for desired_key, entry in state.get("desired_libraries", {}).items():
        if not isinstance(entry, dict):
            continue
        local_info = state.get("local_paths", {}).get(desired_key, {})
        local_path = local_info.get("actual_local_path") if isinstance(local_info, dict) else ""
        matched = _find_library_by_name_or_path(libs, entry.get("library_name", ""), local_path or "")
        if matched:
            entry["exists_in_emby"] = True
            entry["emby_library_id"] = matched.get("id")
            entry["emby_paths"] = matched.get("paths") or []
            if entry.get("last_result") not in {"created", "exists"}:
                entry["last_result"] = "exists"
        else:
            entry["exists_in_emby"] = False
            entry.pop("emby_library_id", None)
            entry.pop("emby_paths", None)
            if entry.get("last_result") == "exists":
                entry["last_result"] = "missing_in_emby"


def _rebuild_memory_cache_locked(state: dict):
    _cache.clear()
    if not _enabled:
        return

    current_server_idx = state.get("settings_snapshot", {}).get("emby_server_idx", _server_idx)
    libs = []
    for value in state.get("emby_libraries", {}).values():
        if not isinstance(value, dict):
            continue
        if value.get("server_idx") != current_server_idx:
            continue
        libs.append({
            "name": value.get("library_name", ""),
            "id": value.get("emby_library_id"),
            "paths": value.get("paths") or [],
            "type": value.get("type", "unknown"),
        })

    for desired_key, entry in state.get("desired_libraries", {}).items():
        if not isinstance(entry, dict):
            continue
        local_info = state.get("local_paths", {}).get(desired_key, {})
        local_path = local_info.get("actual_local_path") if isinstance(local_info, dict) else ""
        matched = _find_library_by_name_or_path(libs, entry.get("library_name", ""), local_path or "")
        if matched:
            _cache[_cache_key(current_server_idx, entry.get("media_type", "unknown"), entry.get("level_key", ""))] = True


def init_cache():
    """应用启动时调用：加载规则、同步持久化状态、刷新 Emby 快照。"""
    global _enabled, _server_idx, _level

    try:
        from app.services.category_matcher import load_rules

        rules = load_rules()
        settings_snapshot = _extract_settings_snapshot((rules or {}).get("sub_classify") or {})
        _enabled = settings_snapshot["sync_emby_library"]
        _server_idx = settings_snapshot["emby_server_idx"]
        _level = settings_snapshot["emby_library_level"]

        with _lock:
            state = load_state()
            state["settings_snapshot"] = settings_snapshot
            state["desired_libraries"] = _build_desired_libraries(rules)
            if _enabled:
                count = _refresh_emby_snapshot_locked(state)
            else:
                count = 0
            _rebuild_memory_cache_locked(state)
            save_state(state)

        if _enabled:
            logger.trace(f"[EmbyLibCache] 已同步 {count} 个 Emby 媒体库快照")
        else:
            logger.trace("[EmbyLibCache] 开关未启用，已同步规则期望状态")
    except Exception as e:
        logger.warning(f"[EmbyLibCache] 初始化失败: {e}")

    if not load_discover_index_cache(_server_idx):
        logger.info("[EmbyLibCache] 未找到 Emby 可用性索引缓存，启动时不自动全量扫描")


def apply_settings(sc: dict):
    """保存子分类设置时调用，更新内存状态并在必要时刷新快照。"""
    global _enabled, _server_idx, _level

    settings_snapshot = _extract_settings_snapshot(sc)
    was_enabled = _enabled
    old_server_idx = _server_idx
    old_level = _level

    _enabled = settings_snapshot["sync_emby_library"]
    _server_idx = settings_snapshot["emby_server_idx"]
    _level = settings_snapshot["emby_library_level"]

    with _lock:
        state = load_state()
        state["settings_snapshot"] = settings_snapshot
        save_state(state)
        if not _enabled:
            _cache.clear()

    if not _enabled:
        logger.info("[EmbyLibCache] 开关已关闭")
        return

    if (not was_enabled) or old_server_idx != _server_idx or old_level != _level:
        refresh_cache()
    else:
        with _lock:
            state = load_state()
            _rebuild_memory_cache_locked(state)
            save_state(state)


def refresh_cache() -> int:
    """刷新 Emby 媒体库快照并重建内存缓存，返回当前选中服务器的媒体库数量。"""
    global _enabled, _server_idx, _level

    from app.services.category_matcher import load_rules

    rules = load_rules()
    settings_snapshot = _extract_settings_snapshot((rules or {}).get("sub_classify") or {})
    _enabled = settings_snapshot["sync_emby_library"]
    _server_idx = settings_snapshot["emby_server_idx"]
    _level = settings_snapshot["emby_library_level"]

    with _lock:
        state = load_state()
        state["settings_snapshot"] = settings_snapshot
        state["desired_libraries"] = _build_desired_libraries(rules)
        count = _refresh_emby_snapshot_locked(state) if _enabled else 0
        _rebuild_memory_cache_locked(state)
        save_state(state)
        return count


def _find_desired_entry_key(state: dict, category_path: str, media_type: Optional[str] = None) -> Optional[str]:
    normalized = normalize_category_path(category_path)
    if not normalized:
        return None

    desired_libraries = state.get("desired_libraries", {})
    if media_type:
        key = _desired_key(media_type, normalized)
        if key in desired_libraries:
            return key

    matches = []
    for key, entry in desired_libraries.items():
        if not isinstance(entry, dict):
            continue
        entry_media_type = entry.get("media_type")
        entry_path = normalize_category_path(entry.get("category_path"))
        if not entry_path:
            continue
        if media_type and entry_media_type != media_type:
            continue
        if normalized == entry_path or normalized.startswith(entry_path + "/"):
            matches.append((len(entry_path.split("/")), key))

    if len(matches) == 1:
        return matches[0][1]
    if len(matches) > 1:
        matches.sort(key=lambda item: item[0], reverse=True)
        top_depth = matches[0][0]
        top_matches = [key for depth, key in matches if depth == top_depth]
        if len(top_matches) > 1:
            logger.warning(f"[EmbyLibCache] 分类路径命中多个同层级规则，使用首个匹配: {normalized}")
        return top_matches[0]
    return None


def ensure_library_if_needed(rel_dir: str, media_type: Optional[str] = None):
    """给定分类相对目录，按粒度检查并在条件满足时创建 Emby 媒体库。"""
    global _enabled, _server_idx, _level

    if not _enabled:
        return

    category_path = normalize_category_path(rel_dir)
    if not category_path:
        return

    with _lock:
        state = load_state()
        desired_key = _find_desired_entry_key(state, category_path, media_type=media_type)
        if not desired_key:
            logger.info(f"[EmbyLibCache] 跳过自动建库，未命中规则: {category_path}")
            save_state(state)
            return

        entry = state["desired_libraries"].get(desired_key, {})
        verify_library_name = entry.get("library_name", category_path.rsplit("/", 1)[-1])
        cache_key = _cache_key(_server_idx, entry.get("media_type", media_type or "unknown"), entry.get("level_key", category_path))
        if cache_key in _cache:
            return

        resolution = _resolve_local_path(entry.get("level_key", category_path))
        local_exists = bool(resolution.get("path") and os.path.isdir(resolution.get("path")))
        local_info = state.setdefault("local_paths", {}).get(desired_key, {})
        if not isinstance(local_info, dict):
            local_info = {}
        local_info.update({
            "actual_local_path": resolution.get("path", ""),
            "exists": local_exists,
            "status": resolution.get("status", "missing_local_path"),
            "desired": True,
            "last_verified_at": _now_ts(),
        })
        state["local_paths"][desired_key] = local_info

        if resolution.get("status") == "ambiguous_local_path":
            logger.info(f"[EmbyLibCache] 跳过自动建库，本地路径存在歧义: {verify_library_name} -> {resolution.get('candidates', [])}")
            entry["last_result"] = "ambiguous_local_path"
            state["desired_libraries"][desired_key] = entry
            save_state(state)
            return

        if not resolution.get("path") or not local_exists:
            logger.info(f"[EmbyLibCache] 跳过自动建库，本地路径不存在: {verify_library_name} -> {resolution.get('path', '')}")
            entry["last_result"] = "missing_local_path"
            state["desired_libraries"][desired_key] = entry
            save_state(state)
            return

        save_state(state)

    with _lock:
        state = load_state()
        desired_key = _find_desired_entry_key(state, category_path, media_type=media_type)
        if not desired_key:
            return
        entry = state["desired_libraries"].get(desired_key, {})
        local_info = state.get("local_paths", {}).get(desired_key, {})
        local_path = local_info.get("actual_local_path", "") if isinstance(local_info, dict) else ""

    result = _create_library(
        server_idx=_server_idx,
        lib_key=entry.get("level_key", category_path),
        lib_name=entry.get("library_name", category_path.rsplit("/", 1)[-1]),
        local_path=local_path,
        media_type=entry.get("media_type", media_type or "unknown"),
    )

    with _lock:
        state = load_state()
        desired_key = _find_desired_entry_key(state, category_path, media_type=media_type)
        if not desired_key:
            return

        entry = state["desired_libraries"].get(desired_key, {})
        local_info = state.setdefault("local_paths", {}).get(desired_key, {})
        if not isinstance(local_info, dict):
            local_info = {}
        if local_path:
            local_info["actual_local_path"] = local_path
        local_info["exists"] = bool(local_path and os.path.isdir(local_path))
        local_info["last_verified_at"] = _now_ts()
        local_info["desired"] = True
        if result.get("status") == "created":
            local_info["last_created_at"] = _now_ts()
        state["local_paths"][desired_key] = local_info

        entry["last_result"] = result.get("status", "create_failed")
        if result.get("library_id"):
            entry["emby_library_id"] = result.get("library_id")
        if result.get("paths"):
            entry["emby_paths"] = result.get("paths")
        if result.get("status") in {"created", "exists"}:
            entry["exists_in_emby"] = True
            _cache[_cache_key(_server_idx, entry.get("media_type", media_type or "unknown"), entry.get("level_key", category_path))] = True
        state["desired_libraries"][desired_key] = entry

        if result.get("snapshot"):
            snapshot = result["snapshot"]
            lib_snapshot_key = f"{_server_idx}:{snapshot.get('id') or snapshot.get('name') or desired_key}"
            snapshot_state = _snapshot_lib_from_live(
                _server_idx,
                {
                    "name": snapshot.get("name", entry.get("library_name", "")),
                    "id": snapshot.get("id"),
                    "type": snapshot.get("type", _guess_collection_type(entry.get("level_key", category_path), entry.get("media_type"))),
                    "paths": snapshot.get("paths") or [local_path],
                },
                state,
            )
            snapshot_state["matched_desired_keys"] = sorted(set(snapshot_state.get("matched_desired_keys", []) + [desired_key]))
            state.setdefault("emby_libraries", {})[lib_snapshot_key] = snapshot_state

        save_state(state)


def _truncate_to_level(rel_dir: str, level: str) -> str:
    parts = normalize_category_path(rel_dir).split("/") if normalize_category_path(rel_dir) else []
    if level == "level1":
        return parts[0] if parts else ""
    if level == "level2":
        return "/".join(parts[:2]) if len(parts) >= 2 else (parts[0] if parts else "")
    if level == "level3":
        return "/".join(parts[:3]) if len(parts) >= 3 else normalize_category_path(rel_dir)
    return normalize_category_path(rel_dir)


def _create_library(server_idx: int, lib_key: str, lib_name: str, local_path: str, media_type: str = "unknown") -> dict:
    try:
        if not local_path or not os.path.isdir(local_path):
            logger.warning(f"[EmbyLibCache] 路径不存在，跳过: {lib_name} -> {local_path}")
            return {"status": "missing_local_path", "local_path": local_path}

        collection_type = _guess_collection_type(lib_key, media_type)
        client = _create_client(server_idx)
        if not client:
            return {"status": "settings_error", "message": f"未找到 Emby 服务器配置: {server_idx}"}

        lib_id, is_new = client.ensure_library_exists(
            name=lib_name,
            path=local_path,
            collection_type=collection_type,
            enable_scrapers=_emby_scrapers_enabled(),
        )
        if lib_id and is_new:
            client.refresh_library(lib_id)
            logger.info(f"[EmbyLibCache] 创建媒体库: {lib_name} -> {local_path}")
        elif lib_id:
            logger.info(f"[EmbyLibCache] 媒体库已存在: {lib_name}")

        if lib_id:
            libs = _collect_server_libraries(client)
            snapshot = next((lib for lib in libs if str(lib.get("id")) == str(lib_id) or lib.get("name") == lib_name), None)
            return {
                "status": "created" if is_new else "exists",
                "library_id": lib_id,
                "paths": (snapshot or {}).get("paths") or [local_path],
                "snapshot": snapshot,
            }
        return {"status": "create_failed", "local_path": local_path}
    except Exception as e:
        logger.warning(f"[EmbyLibCache] 创建失败: {lib_name} -> {e}")
        return {"status": "create_failed", "local_path": local_path, "message": str(e)}


def _resolve_local_path(lib_key: str) -> dict:
    """遍历 STRM 任务，保守解析本地路径。"""
    try:
        config = _read_json_file(STRM_CONFIG_FILE, {})
        tasks = config.get("sync_tasks", []) if isinstance(config, dict) else []
        candidates = []
        seen = set()

        for task in tasks:
            if not isinstance(task, dict):
                continue
            local_root = str(task.get("local_path", "") or "").strip()
            if not local_root:
                continue
            candidate_path = os.path.normpath(os.path.join(local_root, normalize_category_path(lib_key).replace("/", os.sep)))
            if candidate_path in seen:
                continue
            seen.add(candidate_path)
            candidates.append({
                "task_name": task.get("name", "未知任务"),
                "path": candidate_path,
                "exists": os.path.isdir(candidate_path),
            })

        existing_candidates = [item for item in candidates if item["exists"]]
        if len(existing_candidates) == 1:
            return {
                "path": existing_candidates[0]["path"],
                "status": "ok",
                "candidates": candidates,
            }
        if len(existing_candidates) > 1:
            return {
                "path": "",
                "status": "ambiguous_local_path",
                "candidates": candidates,
            }
        if len(candidates) == 1:
            return {
                "path": candidates[0]["path"],
                "status": "missing_local_path",
                "candidates": candidates,
            }
        if len(candidates) > 1:
            return {
                "path": "",
                "status": "ambiguous_local_path",
                "candidates": candidates,
            }
    except Exception as e:
        logger.debug(f"[EmbyLibCache] 解析本地路径失败: {e}")

    return {
        "path": "",
        "status": "missing_local_path",
        "candidates": [],
    }


def _guess_collection_type(path: str, media_type: Optional[str] = None) -> str:
    if media_type == "movie":
        return "movies"
    if media_type == "tv":
        return "tvshows"
    for kw in ("电影", "演唱会", "晚会"):
        if kw in path:
            return "movies"
    return "tvshows"


def _normalize_for_discover(title: str) -> str:
    from app.routers.discover import _normalize_library_title, _extract_season_from_title
    clean, _ = _extract_season_from_title(title)
    clean = clean or title
    for prefix in ("电视剧", "电影", "纪录片", "综艺节目", "综艺"):
        if clean.startswith(prefix):
            clean = clean[len(prefix):].strip()
            break
    return _normalize_library_title(clean)


def _serialize_discover_series_index(series_index: dict[str, dict[int, set[int]]]) -> dict:
    result = {}
    for tmdb_id, seasons in (series_index or {}).items():
        if not isinstance(seasons, dict):
            continue
        season_map = {}
        for season, episodes in seasons.items():
            try:
                season_key = str(int(season))
                season_map[season_key] = sorted({int(ep) for ep in episodes})
            except Exception:
                continue
        result[str(tmdb_id)] = season_map
    return result


def _deserialize_discover_series_index(raw: dict) -> dict[str, dict[int, set[int]]]:
    result: dict[str, dict[int, set[int]]] = {}
    if not isinstance(raw, dict):
        return result
    for tmdb_id, seasons in raw.items():
        if not tmdb_id or not isinstance(seasons, dict):
            continue
        season_map: dict[int, set[int]] = {}
        for season, episodes in seasons.items():
            try:
                season_num = int(season)
            except Exception:
                continue
            if not isinstance(episodes, list):
                continue
            episode_set = set()
            for ep in episodes:
                try:
                    episode_set.add(int(ep))
                except Exception:
                    continue
            season_map[season_num] = episode_set
        result[str(tmdb_id)] = season_map
    return result


def _normalize_episode_counts_for_discover(seasons: dict | None) -> dict[int, set[int]]:
    result: dict[int, set[int]] = {}
    if not isinstance(seasons, dict):
        return result
    for season, episodes in seasons.items():
        try:
            season_num = int(season)
        except Exception:
            continue
        if season_num <= 0:
            continue
        episode_set = set()
        for ep in episodes or []:
            try:
                ep_num = int(ep)
            except Exception:
                continue
            if ep_num > 0:
                episode_set.add(ep_num)
        if episode_set:
            result[season_num] = episode_set
    return result


def _discover_payload_server_idx(payload: dict) -> int:
    meta = payload.get("_meta") if isinstance(payload, dict) else {}
    try:
        return int((meta or {}).get("server_idx", 0) or 0)
    except Exception:
        return 0


def _json_compact(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _create_discover_cache_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS discover_index_meta (
            server_idx INTEGER PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at REAL NOT NULL DEFAULT 0,
            meta_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS discover_index_keys (
            server_idx INTEGER NOT NULL,
            lookup_key TEXT NOT NULL,
            target_value TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (server_idx, lookup_key)
        );

        CREATE INDEX IF NOT EXISTS idx_discover_index_keys_value
            ON discover_index_keys(server_idx, target_value);

        CREATE TABLE IF NOT EXISTS discover_items (
            server_idx INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            tmdb_id TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT '',
            library_id TEXT NOT NULL DEFAULT '',
            emby_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            original_title TEXT NOT NULL DEFAULT '',
            year TEXT NOT NULL DEFAULT '',
            library_name TEXT NOT NULL DEFAULT '',
            item_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (server_idx, item_key)
        );

        CREATE INDEX IF NOT EXISTS idx_discover_items_type
            ON discover_items(server_idx, media_type);
        CREATE INDEX IF NOT EXISTS idx_discover_items_tmdb_type
            ON discover_items(server_idx, tmdb_id, media_type);
        CREATE INDEX IF NOT EXISTS idx_discover_items_emby
            ON discover_items(server_idx, emby_id);

        CREATE TABLE IF NOT EXISTS discover_series_index (
            server_idx INTEGER NOT NULL,
            series_key TEXT NOT NULL,
            tmdb_id TEXT NOT NULL DEFAULT '',
            seasons_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (server_idx, series_key)
        );

        CREATE INDEX IF NOT EXISTS idx_discover_series_tmdb
            ON discover_series_index(server_idx, tmdb_id);
        """
    )


def _save_discover_index_payload_to_db(conn, payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    if not meta:
        return
    server_idx = _discover_payload_server_idx(payload)
    version = int(meta.get("version", DISCOVER_INDEX_VERSION) or DISCOVER_INDEX_VERSION)
    updated_at = float(meta.get("updated_at", 0) or time.time())
    index = payload.get("discover_index") or {}
    items = payload.get("items") or {}
    series_index = payload.get("series_index") or {}

    conn.execute("DELETE FROM discover_index_keys WHERE server_idx = ?", (server_idx,))
    conn.execute("DELETE FROM discover_items WHERE server_idx = ?", (server_idx,))
    conn.execute("DELETE FROM discover_series_index WHERE server_idx = ?", (server_idx,))
    conn.execute(
        """
        INSERT INTO discover_index_meta(server_idx, version, updated_at, meta_json)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(server_idx) DO UPDATE SET
            version = excluded.version,
            updated_at = excluded.updated_at,
            meta_json = excluded.meta_json
        """,
        (server_idx, version, updated_at, _json_compact(meta)),
    )
    conn.executemany(
        """
        INSERT INTO discover_index_keys(server_idx, lookup_key, target_value)
        VALUES(?, ?, ?)
        ON CONFLICT(server_idx, lookup_key) DO UPDATE SET target_value = excluded.target_value
        """,
        [
            (server_idx, str(key), str(value))
            for key, value in (index or {}).items()
            if str(key or "") and str(value or "")
        ],
    )

    item_rows = []
    for item_key, item in (items or {}).items():
        if not isinstance(item, dict):
            continue
        item_key = str(item_key or "")
        if not item_key:
            continue
        item_rows.append((
            server_idx,
            item_key,
            str(item.get("tmdb_id", "") or ""),
            str(item.get("media_type", "") or ""),
            str(item.get("library_id", "") or ""),
            str(item.get("emby_id", "") or ""),
            str(item.get("title", "") or ""),
            str(item.get("original_title", "") or ""),
            str(item.get("year", "") or ""),
            str(item.get("library_name", "") or ""),
            _json_compact(item),
        ))
    conn.executemany(
        """
        INSERT INTO discover_items(
            server_idx, item_key, tmdb_id, media_type, library_id, emby_id,
            title, original_title, year, library_name, item_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(server_idx, item_key) DO UPDATE SET
            tmdb_id = excluded.tmdb_id,
            media_type = excluded.media_type,
            library_id = excluded.library_id,
            emby_id = excluded.emby_id,
            title = excluded.title,
            original_title = excluded.original_title,
            year = excluded.year,
            library_name = excluded.library_name,
            item_json = excluded.item_json
        """,
        item_rows,
    )

    series_rows = []
    for series_key, seasons in (series_index or {}).items():
        series_key = str(series_key or "")
        if not series_key or not isinstance(seasons, dict):
            continue
        tmdb_id = series_key.split(":", 1)[0]
        series_rows.append((server_idx, series_key, tmdb_id, _json_compact(seasons)))
    conn.executemany(
        """
        INSERT INTO discover_series_index(server_idx, series_key, tmdb_id, seasons_json)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(server_idx, series_key) DO UPDATE SET
            tmdb_id = excluded.tmdb_id,
            seasons_json = excluded.seasons_json
        """,
        series_rows,
    )


def _load_discover_index_payload_from_db(conn, server_idx: int | None = None) -> dict:
    if server_idx is None:
        meta_row = conn.execute(
            "SELECT * FROM discover_index_meta ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    else:
        meta_row = conn.execute(
            "SELECT * FROM discover_index_meta WHERE server_idx = ?",
            (int(server_idx or 0),),
        ).fetchone()
    if not meta_row:
        return {}
    server_idx = int(meta_row["server_idx"] or 0)
    try:
        meta = json.loads(meta_row["meta_json"] or "{}")
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    meta.setdefault("version", int(meta_row["version"] or DISCOVER_INDEX_VERSION))
    meta.setdefault("updated_at", float(meta_row["updated_at"] or 0))
    meta.setdefault("server_idx", server_idx)

    index_rows = conn.execute(
        "SELECT lookup_key, target_value FROM discover_index_keys WHERE server_idx = ?",
        (server_idx,),
    ).fetchall()
    item_rows = conn.execute(
        "SELECT item_key, item_json FROM discover_items WHERE server_idx = ?",
        (server_idx,),
    ).fetchall()
    series_rows = conn.execute(
        "SELECT series_key, seasons_json FROM discover_series_index WHERE server_idx = ?",
        (server_idx,),
    ).fetchall()

    discover_index = {str(row["lookup_key"]): str(row["target_value"]) for row in index_rows}
    items = {}
    for row in item_rows:
        try:
            item = json.loads(row["item_json"] or "{}")
            if isinstance(item, dict):
                items[str(row["item_key"])] = item
        except Exception:
            continue
    series_index = {}
    for row in series_rows:
        try:
            seasons = json.loads(row["seasons_json"] or "{}")
            if isinstance(seasons, dict):
                series_index[str(row["series_key"])] = seasons
        except Exception:
            continue
    return {
        "_meta": meta,
        "discover_index": discover_index,
        "series_index": series_index,
        "items": items,
    }


def _migrate_discover_index_json_if_needed(conn) -> None:
    row = conn.execute("SELECT COUNT(*) AS count FROM discover_index_meta").fetchone()
    if row and int(row["count"] or 0) > 0:
        return
    if not os.path.exists(EMBY_DISCOVER_INDEX_FILE):
        return
    payload = _read_json_file(EMBY_DISCOVER_INDEX_FILE, {})
    if not isinstance(payload, dict) or not payload:
        return
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    if int(meta.get("version", 0) or 0) != DISCOVER_INDEX_VERSION:
        return
    started = time.time()
    _save_discover_index_payload_to_db(conn, payload)
    logger.info(
        f"[EmbyLibCache] 已迁移 Emby 可用性索引 JSON 到 SQLite: "
        f"items={len(payload.get('items') or {})} keys={len(payload.get('discover_index') or {})} "
        f"耗时 {time.time() - started:.1f}s"
    )


def _ensure_discover_cache_schema() -> None:
    global _discover_cache_db_ready
    if _discover_cache_db_ready:
        return
    with _discover_cache_db_lock:
        if _discover_cache_db_ready:
            return
        with cache_db(write=True) as conn:
            _create_discover_cache_schema(conn)
            _migrate_discover_index_json_if_needed(conn)
        _discover_cache_db_ready = True


def _load_discover_index_file(server_idx: int | None = None) -> dict:
    try:
        _ensure_discover_cache_schema()
        with cache_db() as conn:
            return _load_discover_index_payload_from_db(conn, server_idx)
    except Exception as e:
        logger.warning(f"[EmbyLibCache] Emby 可用性索引 SQLite 读取失败: {e}")
        return {}


def _save_discover_index_file(payload: dict) -> None:
    _ensure_discover_cache_schema()
    with cache_db(write=True) as conn:
        _save_discover_index_payload_to_db(conn, payload)


def _apply_discover_index_cache(payload: dict, expected_server_idx: int) -> bool:
    global _discover_index, _discover_series_index, _discover_items, _discover_index_meta, _discover_index_built, _discover_index_last_finished_at
    meta = payload.get("_meta") if isinstance(payload, dict) else {}
    if not isinstance(meta, dict) or meta.get("version") != DISCOVER_INDEX_VERSION:
        return False
    if int(meta.get("server_idx", 0) or 0) != int(expected_server_idx or 0):
        return False
    index = payload.get("discover_index") or {}
    items = payload.get("items") or {}
    if not isinstance(index, dict) or not isinstance(items, dict):
        return False
    series_index = _deserialize_discover_series_index(payload.get("series_index") or {})
    with _discover_index_lock:
        _discover_index = {str(k): str(v) for k, v in index.items() if k and v}
        _discover_series_index = series_index
        _discover_items = {str(k): v for k, v in items.items() if isinstance(v, dict)}
        _discover_index_meta = dict(meta)
        _discover_index_built = True
        _discover_index_last_finished_at = float(meta.get("updated_at", 0) or 0)
    return True


def load_discover_index_cache(server_idx: int = 0) -> bool:
    payload = _load_discover_index_file(server_idx)
    if not payload:
        return False
    loaded = _apply_discover_index_cache(payload, server_idx)
    if loaded:
        meta = payload.get("_meta") or {}
        logger.trace(
            f"[EmbyLibCache] 已加载 Emby 可用性索引缓存: keys={meta.get('index_key_count', 0)} "
            f"series={meta.get('series_count', 0)} updated_at={meta.get('updated_at', 0)}"
        )
    return loaded


def get_discover_index_meta() -> dict:
    with _discover_index_lock:
        return dict(_discover_index_meta)


def _current_discover_server_idx() -> int:
    try:
        with _discover_index_lock:
            if _discover_index_meta:
                return int(_discover_index_meta.get("server_idx", _server_idx) or 0)
    except Exception:
        pass
    try:
        return int(_server_idx or 0)
    except Exception:
        return 0


def _row_item_json(row) -> dict | None:
    if not row:
        return None
    try:
        item = json.loads(row["item_json"] or "{}")
        return item if isinstance(item, dict) else None
    except Exception:
        return None


def _series_seasons_from_json(raw: str) -> dict[int, set[int]]:
    try:
        seasons = json.loads(raw or "{}")
    except Exception:
        seasons = {}
    return _deserialize_discover_series_index({"_": seasons}).get("_", {})


def get_discover_item(tmdb_id: str | int | None, media_type: str) -> dict | None:
    key = f"{str(tmdb_id or '').strip()}:{media_type}"
    try:
        _ensure_discover_cache_schema()
        server_idx = _current_discover_server_idx()
        with cache_db() as conn:
            row = conn.execute(
                "SELECT item_json FROM discover_items WHERE server_idx = ? AND item_key = ? LIMIT 1",
                (server_idx, key),
            ).fetchone()
            item = _row_item_json(row)
            if item:
                return dict(item)
            row = conn.execute(
                """
                SELECT item_json FROM discover_items
                WHERE server_idx = ? AND item_key >= ? AND item_key < ?
                ORDER BY item_key LIMIT 1
                """,
                (server_idx, key + ":", key + ";"),
            ).fetchone()
            item = _row_item_json(row)
            if item:
                return dict(item)
    except Exception as e:
        logger.debug(f"[EmbyLibCache] SQLite 读取 discover item 失败: {e}")
    with _discover_index_lock:
        item = _discover_items.get(key)
        if not isinstance(item, dict):
            prefix = f"{key}:"
            item = next((value for item_key, value in _discover_items.items() if item_key.startswith(prefix) and isinstance(value, dict)), None)
        return dict(item) if isinstance(item, dict) else None


def get_discover_series_entries() -> list[dict]:
    try:
        _ensure_discover_cache_schema()
        server_idx = _current_discover_server_idx()
        with cache_db() as conn:
            item_rows = conn.execute(
                """
                SELECT item_key, item_json FROM discover_items
                WHERE server_idx = ? AND media_type = 'tv'
                """,
                (server_idx,),
            ).fetchall()
            series_rows = conn.execute(
                "SELECT series_key, seasons_json FROM discover_series_index WHERE server_idx = ?",
                (server_idx,),
            ).fetchall()
        series_map = {
            str(row["series_key"]): _series_seasons_from_json(row["seasons_json"])
            for row in series_rows
        }
        entries = []
        for row in item_rows:
            key = str(row["item_key"] or "")
            item = _row_item_json(row)
            if not key or not isinstance(item, dict):
                continue
            tmdb_id = key.split(":", 1)[0]
            seasons = series_map.get(key) or series_map.get(tmdb_id, {})
            normalized_seasons = {}
            for season, episodes in (seasons or {}).items():
                try:
                    normalized_seasons[str(int(season))] = sorted({int(ep) for ep in episodes})
                except Exception:
                    continue
            entries.append({
                "tmdb_id": str(tmdb_id),
                "emby_id": item.get("emby_id", ""),
                "title": item.get("title", ""),
                "original_title": item.get("original_title", ""),
                "year": item.get("year", ""),
                "library_id": item.get("library_id", ""),
                "library_name": item.get("library_name", "") or "未分类媒体库",
                "media_type": "tv",
                "seasons": normalized_seasons,
            })
        entries.sort(key=lambda item: (item.get("library_name") or "", item.get("title") or "", item.get("tmdb_id") or ""))
        return entries
    except Exception as e:
        logger.debug(f"[EmbyLibCache] SQLite 读取剧集发现索引失败: {e}")
    with _discover_index_lock:
        entries = []
        for key, item in _discover_items.items():
            parts = key.split(":")
            if len(parts) < 2 or parts[1] != "tv" or not isinstance(item, dict):
                continue
            tmdb_id = parts[0]
            seasons = _discover_series_index.get(key) or _discover_series_index.get(tmdb_id, {})
            normalized_seasons = {}
            for season, episodes in (seasons or {}).items():
                try:
                    normalized_seasons[str(int(season))] = sorted({int(ep) for ep in episodes})
                except Exception:
                    continue
            entries.append({
                "tmdb_id": str(tmdb_id),
                "emby_id": item.get("emby_id", ""),
                "title": item.get("title", ""),
                "original_title": item.get("original_title", ""),
                "year": item.get("year", ""),
                "library_id": item.get("library_id", ""),
                "library_name": item.get("library_name", "") or "未分类媒体库",
                "media_type": "tv",
                "seasons": normalized_seasons,
            })
        return entries


def get_discover_movie_entries() -> list[dict]:
    try:
        _ensure_discover_cache_schema()
        server_idx = _current_discover_server_idx()
        with cache_db() as conn:
            item_rows = conn.execute(
                """
                SELECT item_key, item_json FROM discover_items
                WHERE server_idx = ? AND media_type = 'movie'
                """,
                (server_idx,),
            ).fetchall()
        entries = []
        for row in item_rows:
            key = str(row["item_key"] or "")
            item = _row_item_json(row)
            if not key or not isinstance(item, dict):
                continue
            tmdb_id = key.split(":", 1)[0]
            entries.append({
                "tmdb_id": str(tmdb_id),
                "emby_id": item.get("emby_id", ""),
                "title": item.get("title", ""),
                "original_title": item.get("original_title", ""),
                "year": item.get("year", ""),
                "library_id": item.get("library_id", ""),
                "library_name": item.get("library_name", "") or "电影库",
                "media_type": "movie",
            })
        entries.sort(key=lambda item: (item.get("library_name") or "", item.get("title") or "", item.get("tmdb_id") or ""))
        return entries
    except Exception as e:
        logger.debug(f"[EmbyLibCache] SQLite 读取电影发现索引失败: {e}")
    with _discover_index_lock:
        entries = []
        for key, item in _discover_items.items():
            parts = key.split(":")
            if len(parts) < 2 or parts[1] != "movie" or not isinstance(item, dict):
                continue
            tmdb_id = parts[0]
            entries.append({
                "tmdb_id": str(tmdb_id),
                "emby_id": item.get("emby_id", ""),
                "title": item.get("title", ""),
                "original_title": item.get("original_title", ""),
                "year": item.get("year", ""),
                "library_id": item.get("library_id", ""),
                "library_name": item.get("library_name", "") or "电影库",
                "media_type": "movie",
            })
        return entries


def _build_discover_series_entry_from_item(key: str, item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    parts = key.split(":")
    if len(parts) < 2 or parts[1] != "tv":
        return None
    tmdb_id = parts[0]
    seasons = _discover_series_index.get(key) or _discover_series_index.get(tmdb_id, {})
    normalized_seasons = {}
    for season, episodes in (seasons or {}).items():
        try:
            normalized_seasons[str(int(season))] = sorted({int(ep) for ep in episodes})
        except Exception:
            continue
    return {
        "tmdb_id": str(tmdb_id),
        "emby_id": item.get("emby_id", ""),
        "title": item.get("title", ""),
        "original_title": item.get("original_title", ""),
        "year": item.get("year", ""),
        "library_id": item.get("library_id", ""),
        "library_name": item.get("library_name", "") or "未分类媒体库",
        "media_type": "tv",
        "seasons": normalized_seasons,
    }


def upsert_discover_series_entry(
    *,
    server_idx: int = 0,
    library_id: str = "",
    library_name: str = "",
    emby_id: str = "",
    tmdb_id: str = "",
    title: str = "",
    original_title: str = "",
    year: str = "",
    seasons: dict | None = None,
) -> dict | None:
    """Incrementally update one Series in the persisted discover index."""
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return None
    library_id = str(library_id or "").strip()
    discover_key = f"{tmdb_id}:tv:{library_id}" if library_id else f"{tmdb_id}:tv"
    item = {
        "emby_id": str(emby_id or ""),
        "tmdb_id": tmdb_id,
        "media_type": "tv",
        "title": str(title or ""),
        "original_title": str(original_title or ""),
        "year": str(year or "")[:4],
        "library_id": library_id,
        "library_name": str(library_name or "") or "未分类媒体库",
    }
    season_map = _normalize_episode_counts_for_discover(seasons)
    now = _now_ts()
    with _discover_index_lock:
        _discover_items[discover_key] = item
        _discover_series_index[discover_key] = season_map
        _discover_series_index[tmdb_id] = season_map
        _discover_index[f"tmdb:{tmdb_id}:tv"] = tmdb_id
        for field in ("title", "original_title"):
            norm = _normalize_for_discover(item.get(field, "") or "")
            if not norm:
                continue
            if item.get("year"):
                _discover_index[f"title:{norm}:{item['year']}:tv"] = tmdb_id
            _discover_index.setdefault(f"title:{norm}:tv", tmdb_id)
        _discover_index_meta.update({
            "version": DISCOVER_INDEX_VERSION,
            "updated_at": now,
            "server_idx": int(server_idx or 0),
            "item_count": len(_discover_items),
            "index_key_count": len(_discover_index),
            "series_count": len(_discover_series_index),
            "reason": "webhook:incremental_series",
        })
        payload = {
            "_meta": dict(_discover_index_meta),
            "discover_index": dict(_discover_index),
            "series_index": _serialize_discover_series_index(_discover_series_index),
            "items": dict(_discover_items),
        }
        entry = _build_discover_series_entry_from_item(discover_key, item)
    try:
        _save_discover_index_file(payload)
    except Exception as e:
        logger.warning(f"[EmbyLibCache] Emby 可用性索引增量落盘失败: {e}")
    publish_realtime_event("discover_index_updated", {
        "action": "added",
        "media_type": "tv",
        "tmdb_id": tmdb_id,
        "library_id": library_id,
        "emby_id": str(emby_id or ""),
        "exists": True,
    })
    return entry


def upsert_discover_movie_entry(
    *,
    server_idx: int = 0,
    library_id: str = "",
    library_name: str = "",
    emby_id: str = "",
    tmdb_id: str = "",
    title: str = "",
    original_title: str = "",
    year: str = "",
) -> bool:
    """Incrementally update one Movie in the persisted discover index."""
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return False
    item = {
        "emby_id": str(emby_id or ""),
        "tmdb_id": tmdb_id,
        "media_type": "movie",
        "title": str(title or ""),
        "original_title": str(original_title or ""),
        "year": str(year or "")[:4],
        "library_id": str(library_id or "").strip(),
        "library_name": str(library_name or "") or "未分类媒体库",
    }
    now = _now_ts()
    discover_key = f"{tmdb_id}:movie"
    with _discover_index_lock:
        _discover_items[discover_key] = item
        _discover_index[f"tmdb:{tmdb_id}:movie"] = tmdb_id
        for field in ("title", "original_title"):
            norm = _normalize_for_discover(item.get(field, "") or "")
            if not norm:
                continue
            if item.get("year"):
                _discover_index[f"title:{norm}:{item['year']}:movie"] = tmdb_id
            _discover_index.setdefault(f"title:{norm}:movie", tmdb_id)
        _discover_index_meta.update({
            "version": DISCOVER_INDEX_VERSION,
            "updated_at": now,
            "server_idx": int(server_idx or 0),
            "item_count": len(_discover_items),
            "index_key_count": len(_discover_index),
            "series_count": len(_discover_series_index),
            "reason": "webhook:incremental_movie",
        })
        payload = {
            "_meta": dict(_discover_index_meta),
            "discover_index": dict(_discover_index),
            "series_index": _serialize_discover_series_index(_discover_series_index),
            "items": dict(_discover_items),
        }
    try:
        _save_discover_index_file(payload)
    except Exception as e:
        logger.warning(f"[EmbyLibCache] Emby 可用性索引电影增量落盘失败: {e}")
        return False
    publish_realtime_event("discover_index_updated", {
        "action": "added",
        "media_type": "movie",
        "tmdb_id": tmdb_id,
        "library_id": item.get("library_id", ""),
        "emby_id": str(emby_id or ""),
        "exists": True,
    })
    return True


def remove_discover_movie_entry(*, tmdb_id: str = "", library_id: str = "") -> bool:
    """Remove one Movie from the discover index when webhook has enough identity data."""
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return False
    removed = False
    now = _now_ts()
    with _discover_index_lock:
        discover_key = f"{tmdb_id}:movie"
        if discover_key in _discover_items:
            removed = True
            _discover_items.pop(discover_key, None)
        _discover_index.pop(f"tmdb:{tmdb_id}:movie", None)
        stale_title_keys = [
            key for key, value in _discover_index.items()
            if str(value) == tmdb_id and str(key).endswith(":movie")
        ]
        for key in stale_title_keys:
            _discover_index.pop(key, None)
        if not removed:
            return False
        _discover_index_meta.update({
            "version": DISCOVER_INDEX_VERSION,
            "updated_at": now,
            "item_count": len(_discover_items),
            "index_key_count": len(_discover_index),
            "series_count": len(_discover_series_index),
            "reason": "webhook:remove_movie",
        })
        payload = {
            "_meta": dict(_discover_index_meta),
            "discover_index": dict(_discover_index),
            "series_index": _serialize_discover_series_index(_discover_series_index),
            "items": dict(_discover_items),
        }
    try:
        _save_discover_index_file(payload)
    except Exception as e:
        logger.warning(f"[EmbyLibCache] Emby 可用性索引电影删除落盘失败: {e}")
    publish_realtime_event("discover_index_updated", {
        "action": "removed",
        "media_type": "movie",
        "tmdb_id": tmdb_id,
        "library_id": str(library_id or ""),
        "exists": False,
    })
    return True


def remove_discover_series_entry(*, tmdb_id: str = "", library_id: str = "") -> bool:
    """Remove one Series from the discover index when webhook has enough identity data."""
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return False
    library_id = str(library_id or "").strip()
    removed = False
    now = _now_ts()
    with _discover_index_lock:
        if library_id:
            candidates = [f"{tmdb_id}:tv:{library_id}", f"{tmdb_id}:tv"]
        else:
            candidates = [
                key for key in _discover_items
                if str(key) == f"{tmdb_id}:tv" or str(key).startswith(f"{tmdb_id}:tv:")
            ]
        for key in candidates:
            if key in _discover_items:
                removed = True
                _discover_items.pop(key, None)
                _discover_series_index.pop(key, None)
        if not any(str(key).startswith(f"{tmdb_id}:tv:") or str(key) == f"{tmdb_id}:tv" for key in _discover_items):
            _discover_series_index.pop(tmdb_id, None)
            _discover_index.pop(f"tmdb:{tmdb_id}:tv", None)
        if not removed:
            return False
        _discover_index_meta.update({
            "version": DISCOVER_INDEX_VERSION,
            "updated_at": now,
            "item_count": len(_discover_items),
            "index_key_count": len(_discover_index),
            "series_count": len(_discover_series_index),
            "reason": "webhook:remove_series",
        })
        payload = {
            "_meta": dict(_discover_index_meta),
            "discover_index": dict(_discover_index),
            "series_index": _serialize_discover_series_index(_discover_series_index),
            "items": dict(_discover_items),
        }
    try:
        _save_discover_index_file(payload)
    except Exception as e:
        logger.warning(f"[EmbyLibCache] Emby 可用性索引删除落盘失败: {e}")
    publish_realtime_event("discover_index_updated", {
        "action": "removed",
        "media_type": "tv",
        "tmdb_id": tmdb_id,
        "library_id": library_id,
        "exists": False,
    })
    return True


def patch_discover_series_episode(*, emby_id: str = "", season: int | str | None = None, episode: int | str | None = None, present: bool = True) -> dict | None:
    """Patch one episode in one cached Series when webhook payload lacks full series metadata."""
    emby_id = str(emby_id or "").strip()
    if not emby_id:
        return None
    try:
        season_num = int(season)
        episode_num = int(episode)
    except Exception:
        return None
    if season_num <= 0 or episode_num <= 0:
        return None
    now = _now_ts()
    with _discover_index_lock:
        hit_key = ""
        hit_item = None
        for key, item in _discover_items.items():
            if isinstance(item, dict) and item.get("media_type") == "tv" and str(item.get("emby_id") or "") == emby_id:
                hit_key = key
                hit_item = item
                break
        if not hit_key or not hit_item:
            return None
        tmdb_id = str(hit_item.get("tmdb_id") or hit_key.split(":")[0])
        season_map = _discover_series_index.get(hit_key) or _discover_series_index.get(tmdb_id, {})
        season_map = {int(s): set(eps or []) for s, eps in (season_map or {}).items()}
        episodes = season_map.setdefault(season_num, set())
        if present:
            episodes.add(episode_num)
        else:
            episodes.discard(episode_num)
            if not episodes:
                season_map.pop(season_num, None)
        _discover_series_index[hit_key] = season_map
        _discover_series_index[tmdb_id] = season_map
        _discover_index_meta.update({
            "version": DISCOVER_INDEX_VERSION,
            "updated_at": now,
            "item_count": len(_discover_items),
            "index_key_count": len(_discover_index),
            "series_count": len(_discover_series_index),
            "reason": "webhook:patch_episode",
        })
        payload = {
            "_meta": dict(_discover_index_meta),
            "discover_index": dict(_discover_index),
            "series_index": _serialize_discover_series_index(_discover_series_index),
            "items": dict(_discover_items),
        }
        entry = _build_discover_series_entry_from_item(hit_key, hit_item)
    try:
        _save_discover_index_file(payload)
    except Exception as e:
        logger.warning(f"[EmbyLibCache] Emby 可用性索引单集落盘失败: {e}")
    return entry


def schedule_discover_index_refresh(server_idx: int = 0, reason: str = "manual", delay_sec: float = 30, force: bool = False) -> None:
    global _discover_index_timer, _discover_index_refresh_pending, _discover_index_pending_reason
    now = time.time()
    with _discover_index_lock:
        if _discover_index_building:
            _discover_index_refresh_pending = True
            _discover_index_pending_reason = reason
            logger.info(f"[EmbyLibCache] Emby 可用性索引刷新中，已合并请求: {reason}")
            return
        if not force and _discover_index_last_finished_at and now - _discover_index_last_finished_at < DISCOVER_INDEX_MIN_REFRESH_INTERVAL:
            logger.info(f"[EmbyLibCache] 跳过 Emby 可用性索引刷新，距离上次刷新不足 5 分钟: {reason}")
            return
        if _discover_index_timer and _discover_index_timer.is_alive():
            if force:
                _discover_index_timer.cancel()
            else:
                logger.info(f"[EmbyLibCache] Emby 可用性索引刷新已在队列中，合并请求: {reason}")
                return
        timer = threading.Timer(max(0.0, float(delay_sec or 0)), build_discover_index, kwargs={
            "server_idx": server_idx,
            "reason": reason,
            "force": force,
        })
        timer.daemon = True
        _discover_index_timer = timer
        timer.start()
        logger.info(f"[EmbyLibCache] 已调度 Emby 可用性索引刷新: reason={reason}, delay={delay_sec}s")


def build_discover_index(server_idx: int = 0, reason: str = "manual", force: bool = False) -> None:
    global _discover_index, _discover_series_index, _discover_items, _discover_index_meta
    global _discover_index_built, _discover_index_building, _discover_index_refresh_pending
    global _discover_index_last_finished_at, _discover_index_pending_reason
    with _discover_index_lock:
        if _discover_index_building:
            _discover_index_refresh_pending = True
            if reason:
                _discover_index_pending_reason = reason
            return
        _discover_index_building = True
    pending_reason = ""
    try:
        start_time = time.time()
        client = _create_client(server_idx)
        if not client:
            return
        libraries = _collect_server_libraries(client)
        tv_libraries = [
            lib for lib in libraries
            if lib.get("id") and str(lib.get("type", "") or "").lower() in {"tvshows", "mixed", "unknown", ""}
        ]
        items: dict[str, dict] = {}
        tv_episode_counts_by_key: dict[str, dict[int, set[int]]] = {}
        tv_library_count = 0
        episode_batch_library_count = 0
        episode_batch_series_count = 0
        library_scan_error_count = 0

        def _scan_movie_items() -> dict:
            scan_client = _create_client(server_idx)
            if not scan_client:
                return {"kind": "movies", "items": {}}
            try:
                all_items = scan_client.get_all_library_items(item_types="Movie")
                return {
                    "kind": "movies",
                    "items": {
                        key: value
                        for key, value in (all_items or {}).items()
                        if isinstance(value, dict) and value.get("media_type") != "tv"
                    },
                }
            finally:
                scan_client.close()

        def _scan_tv_library(lib: dict) -> dict:
            lib_id = lib.get("id")
            lib_name = lib.get("name", "")
            scan_client = _create_client(server_idx)
            if not scan_client:
                return {"kind": "tv", "library_id": lib_id, "library_name": lib_name, "items": {}, "episode_counts": {}}
            try:
                library_items = scan_client.get_all_library_items(
                    item_types="Series",
                    library_id=lib_id,
                    library_name=lib_name,
                )
                episode_counts_by_series_id = (
                    scan_client.get_series_episode_counts_by_library(lib_id)
                    if library_items else {}
                )
                return {
                    "kind": "tv",
                    "library_id": lib_id,
                    "library_name": lib_name,
                    "items": library_items or {},
                    "episode_counts": episode_counts_by_series_id or {},
                }
            finally:
                scan_client.close()

        scan_workers = min(DISCOVER_INDEX_LIBRARY_SCAN_WORKERS, max(1, len(tv_libraries) + 1))
        with ThreadPoolExecutor(max_workers=scan_workers) as executor:
            futures = [executor.submit(_scan_movie_items)]
            futures.extend(executor.submit(_scan_tv_library, lib) for lib in tv_libraries)
            for future in as_completed(futures):
                try:
                    scan_result = future.result() or {}
                except Exception as e:
                    library_scan_error_count += 1
                    logger.warning(f"[EmbyLibCache] Emby 可用性索引媒体库扫描失败: {e}")
                    continue

                if scan_result.get("kind") == "movies":
                    items.update(scan_result.get("items") or {})
                    continue

                library_items = scan_result.get("items") or {}
                if not library_items:
                    continue
                tv_library_count += 1
                items.update(library_items)
                episode_counts_by_series_id = scan_result.get("episode_counts") or {}
                if episode_counts_by_series_id:
                    episode_batch_library_count += 1
                    episode_batch_series_count += len(episode_counts_by_series_id)
                for item_meta in library_items.values():
                    if not isinstance(item_meta, dict):
                        continue
                    tmdb_id = str(item_meta.get("tmdb_id", "") or "")
                    media_type = item_meta.get("media_type", "")
                    series_id = str(item_meta.get("emby_id", "") or "")
                    library_id = str(item_meta.get("library_id") or "")
                    if not tmdb_id or not series_id:
                        continue
                    discover_key = f"{tmdb_id}:{media_type}:{library_id}" if library_id else f"{tmdb_id}:{media_type}"
                    episode_counts = episode_counts_by_series_id.get(series_id)
                    if episode_counts is not None:
                        tv_episode_counts_by_key[discover_key] = episode_counts
        if not any(
            isinstance(value, dict) and value.get("media_type") == "tv"
            for value in items.values()
        ):
            fallback_tv_items = client.get_all_library_items(item_types="Series")
            items.update(fallback_tv_items)
        index: dict[str, str] = {}
        series_index: dict[str, dict[int, set[int]]] = {}
        discover_items: dict[str, dict] = {}
        series_error_count = 0
        series_fallback_count = 0
        for key, meta in items.items():
            if not isinstance(meta, dict):
                continue
            tmdb_id = str(meta.get("tmdb_id", "") or "")
            media_type = meta.get("media_type", "")
            year = str(meta.get("year", "") or "")
            if not tmdb_id:
                continue
            library_id = str(meta.get("library_id") or "")
            discover_key = f"{tmdb_id}:{media_type}:{library_id}" if library_id else f"{tmdb_id}:{media_type}"
            discover_items[discover_key] = dict(meta)
            index[f"tmdb:{tmdb_id}:{media_type}"] = tmdb_id
            for field in ("title", "original_title"):
                norm = _normalize_for_discover(meta.get(field, "") or "")
                if not norm:
                    continue
                if year:
                    index[f"title:{norm}:{year}:{media_type}"] = tmdb_id
                index.setdefault(f"title:{norm}:{media_type}", tmdb_id)
            if media_type == "tv":
                try:
                    episode_counts = tv_episode_counts_by_key.get(discover_key)
                    if episode_counts is None:
                        series_fallback_count += 1
                        episode_counts = client.get_series_episode_counts_by_id(meta.get("emby_id"))
                    series_index[discover_key] = episode_counts
                    series_index.setdefault(tmdb_id, episode_counts)
                except Exception as e:
                    series_error_count += 1
                    logger.debug(f"[EmbyLibCache] 剧集季集索引构建失败 TMDB:{tmdb_id}: {e}")
        now = _now_ts()
        meta = {
            "version": DISCOVER_INDEX_VERSION,
            "updated_at": now,
            "server_idx": int(server_idx or 0),
            "item_count": len(discover_items),
            "index_key_count": len(index),
            "series_count": len(series_index),
            "tv_library_count": tv_library_count,
            "library_scan_workers": scan_workers,
            "library_scan_error_count": library_scan_error_count,
            "episode_batch_library_count": episode_batch_library_count,
            "episode_batch_series_count": episode_batch_series_count,
            "series_fallback_count": series_fallback_count,
            "series_error_count": series_error_count,
            "build_duration_sec": round(time.time() - start_time, 3),
            "reason": reason,
        }
        payload = {
            "_meta": meta,
            "discover_index": index,
            "series_index": _serialize_discover_series_index(series_index),
            "items": discover_items,
        }
        with _discover_index_lock:
            _discover_index = index
            _discover_series_index = series_index
            _discover_items = discover_items
            _discover_index_meta = meta
            _discover_index_built = True
            _discover_index_last_finished_at = time.time()
        try:
            _save_discover_index_file(payload)
        except Exception as e:
            logger.warning(f"[EmbyLibCache] Emby 可用性索引落盘失败: {e}")
        logger.info(
            f"[EmbyLibCache] Emby 可用性索引已构建: {len(index)} 条, {len(series_index)} 部剧集, "
            f"耗时 {meta['build_duration_sec']} 秒, reason={reason}"
        )
    except Exception as e:
        logger.warning(f"[EmbyLibCache] Emby 可用性索引构建失败，沿用旧缓存: {e}")
    finally:
        with _discover_index_lock:
            _discover_index_building = False
            if _discover_index_refresh_pending:
                pending_reason = _discover_index_pending_reason or "pending_after_refresh"
                _discover_index_refresh_pending = False
                _discover_index_pending_reason = ""
        if pending_reason:
            schedule_discover_index_refresh(server_idx=server_idx, reason=pending_reason, delay_sec=60)


def lookup_discover_tmdb_id(title: str, year: str, media_type: str) -> str | None:
    norm = _normalize_for_discover(title)
    if not norm:
        return None
    try:
        _ensure_discover_cache_schema()
        server_idx = _current_discover_server_idx()
        with cache_db() as conn:
            if year:
                row = conn.execute(
                    """
                    SELECT target_value FROM discover_index_keys
                    WHERE server_idx = ? AND lookup_key = ?
                    LIMIT 1
                    """,
                    (server_idx, f"title:{norm}:{year}:{media_type}"),
                ).fetchone()
                if row and row["target_value"]:
                    return str(row["target_value"])
            row = conn.execute(
                """
                SELECT target_value FROM discover_index_keys
                WHERE server_idx = ? AND lookup_key = ?
                LIMIT 1
                """,
                (server_idx, f"title:{norm}:{media_type}"),
            ).fetchone()
            if row and row["target_value"]:
                return str(row["target_value"])
    except Exception as e:
        logger.debug(f"[EmbyLibCache] SQLite 查询标题索引失败: {e}")
    with _discover_index_lock:
        if year:
            result = _discover_index.get(f"title:{norm}:{year}:{media_type}")
            if result:
                return result
        return _discover_index.get(f"title:{norm}:{media_type}")


def discover_tmdb_id_exists(tmdb_id: str | int | None, media_type: str) -> bool:
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return False
    try:
        _ensure_discover_cache_schema()
        server_idx = _current_discover_server_idx()
        with cache_db() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM discover_index_keys
                WHERE server_idx = ? AND lookup_key = ?
                LIMIT 1
                """,
                (server_idx, f"tmdb:{tmdb_id}:{media_type}"),
            ).fetchone()
            if row:
                return True
    except Exception as e:
        logger.debug(f"[EmbyLibCache] SQLite 查询 TMDB 索引失败: {e}")
    with _discover_index_lock:
        return f"tmdb:{tmdb_id}:{media_type}" in _discover_index


def get_discover_series_status(tmdb_id: str | int | None) -> dict:
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return {"exists": False, "seasons": {}}
    try:
        _ensure_discover_cache_schema()
        server_idx = _current_discover_server_idx()
        with cache_db() as conn:
            exists = conn.execute(
                """
                SELECT 1 FROM discover_index_keys
                WHERE server_idx = ? AND lookup_key = ?
                LIMIT 1
                """,
                (server_idx, f"tmdb:{tmdb_id}:tv"),
            ).fetchone() is not None
            row = conn.execute(
                """
                SELECT seasons_json FROM discover_series_index
                WHERE server_idx = ? AND series_key = ?
                LIMIT 1
                """,
                (server_idx, tmdb_id),
            ).fetchone()
            if not row:
                row = conn.execute(
                    """
                    SELECT seasons_json FROM discover_series_index
                    WHERE server_idx = ? AND tmdb_id = ?
                    ORDER BY series_key LIMIT 1
                    """,
                    (server_idx, tmdb_id),
                ).fetchone()
        seasons = _series_seasons_from_json(row["seasons_json"]) if row else {}
        return {
            "exists": exists,
            "seasons": {
                str(season): sorted(episodes)
                for season, episodes in seasons.items()
            },
        }
    except Exception as e:
        logger.debug(f"[EmbyLibCache] SQLite 查询剧集状态失败: {e}")
    with _discover_index_lock:
        seasons = _discover_series_index.get(tmdb_id, {})
        normalized = {
            str(season): sorted(episodes)
            for season, episodes in seasons.items()
        }
        return {
            "exists": f"tmdb:{tmdb_id}:tv" in _discover_index,
            "seasons": normalized,
        }


def get_discover_index_ready() -> bool:
    with _discover_index_lock:
        return _discover_index_built
