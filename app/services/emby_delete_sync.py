import json
import os
import re
import time
from typing import Any

from core.configs import BASE_DIR
from core.logger import logger
from core.media_library_cache import (
    build_task_key,
    get_task_item_by_path,
    remove_items_by_path_prefix,
    remove_task_item_by_id,
)
from app.services.media_organize_115_ops import _get_115_client, run_115_write_request_sync


VIDEO_EXTS = {
    ".mp4", ".mpg", ".mkv", ".mpeg", ".ts", ".vob", ".iso", ".m4v", ".avi", ".3gp", ".wmv", ".webm",
    ".flv", ".mov", ".m2ts", ".rmvb", ".rm", ".asf", ".f4v", ".m2t", ".mts", ".mpe", ".tp", ".trp",
    ".divx", ".ogv", ".dv", ".strm",
}

_RECENT_DELETE_KEYS: dict[str, float] = {}


def _read_json(path: str, default: Any):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _normalize_path(path: str) -> str:
    cleaned = [segment for segment in str(path or "").replace("\\", "/").split("/") if segment]
    return "/" + "/".join(cleaned) if cleaned else ""


def _dirname(path: str) -> str:
    return os.path.dirname(_normalize_path(path)).replace("\\", "/").rstrip("/")


def _is_video_path(path: str) -> bool:
    return os.path.splitext(str(path or "").lower())[1] in VIDEO_EXTS


def _extract_description_item_path(description: str) -> str:
    match = re.search(r"(?:^|\n)Item Path:\s*\n(?P<path>.+?)(?:\n\n|$)", str(description or ""), re.S)
    if not match:
        return ""
    return _normalize_path(match.group("path").strip())


def _load_strm_tasks() -> list[dict]:
    data = _read_json(os.path.join(BASE_DIR, "config", "strm_config.json"), {})
    tasks = data.get("sync_tasks") if isinstance(data, dict) else []
    return [t for t in tasks if isinstance(t, dict)]


def _load_config_302() -> dict:
    return _read_json(os.path.join(BASE_DIR, "config", "config_302.json"), {})


def _load_emby_state() -> dict:
    return _read_json(os.path.join(BASE_DIR, "config", "media_organize_emby_state.json"), {})


def _add_mapping(mappings: list[tuple[str, str]], emby_prefix: str, remote_prefix: str):
    emby_norm = _normalize_path(emby_prefix).rstrip("/")
    remote_norm = _normalize_path(remote_prefix).rstrip("/")
    if emby_norm and remote_norm:
        mappings.append((emby_norm, remote_norm))


def _build_path_mappings() -> list[tuple[str, str]]:
    mappings: list[tuple[str, str]] = []

    tasks = _load_strm_tasks()
    cfg302 = _load_config_302()
    topology = cfg302.get("standard_topology") if isinstance(cfg302, dict) else {}
    if not isinstance(topology, dict):
        topology = {}
    state = _load_emby_state()
    desired = state.get("desired_libraries") if isinstance(state, dict) else {}
    if not isinstance(desired, dict):
        desired = {}

    for task in tasks:
        _add_mapping(mappings, str(task.get("local_path") or ""), str(task.get("remote_path") or ""))

    _add_mapping(mappings, str(topology.get("local_media_dir") or ""), str(topology.get("media_dir") or ""))

    for entry in desired.values():
        if not isinstance(entry, dict):
            continue
        category_path = str(entry.get("category_path") or "").strip("/")
        remote_roots = []
        for task in tasks:
            remote_root = str(task.get("remote_path") or "").rstrip("/")
            if remote_root:
                remote_roots.append(remote_root)
        topology_media_dir = str(topology.get("media_dir") or "").rstrip("/")
        if topology_media_dir:
            remote_roots.append(topology_media_dir)

        for emby_path in entry.get("emby_paths") or []:
            for remote_root in remote_roots:
                remote_prefix = f"{remote_root}/{category_path}" if category_path else remote_root
                _add_mapping(mappings, str(emby_path), remote_prefix)
                emby_norm = _normalize_path(str(emby_path)).rstrip("/")
                if category_path and emby_norm.endswith("/" + category_path):
                    emby_root = emby_norm[:-(len(category_path) + 1)]
                    _add_mapping(mappings, emby_root, remote_root)

    # Longest Emby prefix first, so category-level libraries beat root-level fallbacks.
    deduped = list(dict.fromkeys(mappings))
    deduped.sort(key=lambda item: len(item[0]), reverse=True)
    return deduped


def resolve_emby_path_to_remote_path(emby_path: str) -> dict:
    local_path = _normalize_path(emby_path).rstrip("/")
    if not local_path:
        return {"status": "error", "message": "缺少 Emby 路径", "remote_path": ""}

    for emby_prefix, remote_prefix in _build_path_mappings():
        if local_path == emby_prefix or local_path.startswith(emby_prefix + "/"):
            suffix = local_path[len(emby_prefix):].strip("/")
            remote_path = f"{remote_prefix}/{suffix}" if suffix else remote_prefix
            return {
                "status": "ok",
                "remote_path": _normalize_path(remote_path).rstrip("/"),
                "matched_emby_prefix": emby_prefix,
                "matched_remote_prefix": remote_prefix,
            }

    return {"status": "error", "message": f"未找到 Emby 路径映射: {local_path}", "remote_path": ""}


def _resolve_delete_scope(payload: dict) -> dict:
    event_type = str(payload.get("Event") or "")
    item = payload.get("Item") if isinstance(payload.get("Item"), dict) else {}
    item_type = str(item.get("Type") or "")
    item_path = _normalize_path(str(item.get("Path") or ""))
    desc_path = _extract_description_item_path(str(payload.get("Description") or ""))

    if event_type == "deep.delete":
        source_path = desc_path or item_path
        if item_type == "Movie":
            return {"kind": "folder", "item_type": item_type, "emby_path": _dirname(source_path) if _is_video_path(source_path) else source_path}
        if item_type == "Episode":
            return {"kind": "file", "item_type": item_type, "emby_path": source_path}
        if item_type in ("Season", "Series"):
            return {"kind": "folder", "item_type": item_type, "emby_path": source_path}
        return {"kind": "", "item_type": item_type, "emby_path": source_path, "message": f"不支持的 deep.delete 类型: {item_type}"}

    return {"kind": "", "item_type": item_type, "emby_path": item_path, "message": f"跳过事件: {event_type}/{item_type}"}


def _get_task_for_remote_path(remote_path: str) -> tuple[int, str]:
    remote = _normalize_path(remote_path).rstrip("/")
    best: tuple[int, str] | None = None
    for task in _load_strm_tasks():
        try:
            drive_index = int(task.get("drive_index", 0) or 0)
        except (TypeError, ValueError):
            drive_index = 0
        task_remote = _normalize_path(str(task.get("remote_path") or "")).rstrip("/")
        if task_remote and (remote == task_remote or remote.startswith(task_remote + "/")):
            if best is None or len(task_remote) > len(best[1]):
                best = (drive_index, task_remote)
    if best:
        return best

    cfg302 = _load_config_302()
    topology = cfg302.get("standard_topology") if isinstance(cfg302, dict) else {}
    media_dir = _normalize_path(str((topology or {}).get("media_dir") or "")).rstrip("/")
    if media_dir and (remote == media_dir or remote.startswith(media_dir + "/")):
        return 0, media_dir
    return 0, ""


def _dedupe_key(kind: str, remote_path: str) -> str:
    return f"{kind}:{_normalize_path(remote_path).rstrip('/')}"


def _is_recent_duplicate(key: str, ttl_seconds: int = 300) -> bool:
    now = time.time()
    expired = [k for k, ts in _RECENT_DELETE_KEYS.items() if now - ts > ttl_seconds]
    for k in expired:
        _RECENT_DELETE_KEYS.pop(k, None)
    if key in _RECENT_DELETE_KEYS:
        return True
    _RECENT_DELETE_KEYS[key] = now
    return False


def sync_emby_delete_to_115(payload: dict, config: dict) -> dict:
    if not bool(config.get("delete_sync_enabled", False)):
        return {"status": "disabled"}

    scope = _resolve_delete_scope(payload)
    if not scope.get("kind"):
        return {"status": "skipped", "message": scope.get("message", "未命中删除范围")}

    mapped = resolve_emby_path_to_remote_path(scope.get("emby_path", ""))
    if mapped.get("status") != "ok":
        logger.warning(f"[WebhookDeleteSync] 路径映射失败: {mapped.get('message')}")
        return {"status": "error", "message": mapped.get("message")}

    remote_path = str(mapped.get("remote_path") or "").rstrip("/")
    drive_index, task_remote = _get_task_for_remote_path(remote_path)
    if not task_remote:
        message = f"远端路径未命中 STRM/媒体库任务: {remote_path}"
        logger.warning(f"[WebhookDeleteSync] {message}")
        return {"status": "error", "message": message, "remote_path": remote_path}

    task_key = build_task_key(drive_index, task_remote)
    cache_item = get_task_item_by_path(task_key, remote_path)
    if not cache_item:
        message = f"媒体库缓存未命中，跳过删除: {remote_path}"
        logger.warning(f"[WebhookDeleteSync] {message}")
        return {"status": "cache_miss", "message": message, "remote_path": remote_path}

    expected_dir = scope.get("kind") == "folder"
    cached_is_dir = bool(cache_item.get("is_dir"))
    if expected_dir != cached_is_dir:
        message = f"缓存类型不匹配，跳过删除: expected={scope.get('kind')} cached_is_dir={cached_is_dir} path={remote_path}"
        logger.warning(f"[WebhookDeleteSync] {message}")
        return {"status": "type_mismatch", "message": message, "remote_path": remote_path}

    item_id = int(cache_item.get("id") or 0)
    if item_id <= 0:
        message = f"缓存缺少 115 ID，跳过删除: {remote_path}"
        logger.warning(f"[WebhookDeleteSync] {message}")
        return {"status": "missing_id", "message": message, "remote_path": remote_path}

    delete_label = f"{scope.get('item_type')}/{scope.get('kind')}"
    if _is_recent_duplicate(_dedupe_key(str(scope.get("kind")), remote_path)):
        logger.info(f"[WebhookDeleteSync] 重复删除事件已跳过: {delete_label} {remote_path}")
        return {"status": "duplicate", "remote_path": remote_path, "id": item_id}

    client = _get_115_client(drive_index)
    run_115_write_request_sync(
        client,
        f"Webhook同步删除{delete_label}",
        lambda write_client: write_client.fs_delete([item_id], async_=False),
    )

    if scope.get("kind") == "folder":
        removed_cache = remove_items_by_path_prefix(
            task_key,
            remote_path,
            meta={"last_status": "deleted_by_emby_webhook"},
        )
    else:
        removed_cache = remove_task_item_by_id(
            task_key,
            item_id,
            meta={"last_status": "deleted_by_emby_webhook"},
        )

    logger.info(f"[WebhookDeleteSync] 已同步删除 115 {delete_label}: id={item_id} path={remote_path} cache_removed={removed_cache}")
    return {
        "status": "deleted",
        "remote_path": remote_path,
        "id": item_id,
        "kind": scope.get("kind"),
        "cache_removed": removed_cache,
    }
