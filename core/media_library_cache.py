import json
import os
import threading
import time

from core.configs import MEDIA_LIBRARY_CACHE_FILE

_lock = threading.RLock()

# pickcode 索引：lazy invalidation，mutation 时标记 dirty，读取时按需重建
_pickcode_index: dict[str, dict] | None = None
_index_dirty = True

_resident_task_indexes: dict[str, "MediaLibraryTaskIndex"] = {}
_cache_file_token: tuple[int, int] | None = None

_VIDEO_EXTS = {
    '.mp4', '.mpg', '.mkv', '.mpeg', '.ts', '.vob', '.iso', '.m4v', '.avi', '.3gp', '.wmv', '.webm',
    '.flv', '.mov', '.m2ts', '.rmvb', '.rm', '.asf', '.f4v', '.m2t', '.mts', '.mpe', '.tp', '.trp',
    '.divx', '.ogv', '.dv'
}
_PARSE_FILENAME_FUNC = None


def _default_cache() -> dict:
    return {
        "_meta": {
            "version": 1,
            "updated_at": 0,
        },
        "tasks": {},
    }


def build_task_key(drive_index: int, remote_path: str) -> str:
    return f"{drive_index}:{str(remote_path or '').rstrip('/')}"


def _stat_cache_file_token() -> tuple[int, int]:
    try:
        stat = os.stat(MEDIA_LIBRARY_CACHE_FILE)
        return int(stat.st_mtime_ns), int(stat.st_size)
    except OSError:
        return 0, 0


def _set_cache_file_token_locked():
    global _cache_file_token
    _cache_file_token = _stat_cache_file_token()


def _drop_resident_indexes_if_file_changed_locked():
    global _cache_file_token, _pickcode_index, _index_dirty
    current_token = _stat_cache_file_token()
    if _cache_file_token is None:
        _cache_file_token = current_token
        return
    if current_token != _cache_file_token:
        _resident_task_indexes.clear()
        _pickcode_index = None
        _index_dirty = True
        _cache_file_token = current_token


def _normalize_remote_path(path: str) -> str:
    cleaned = [segment for segment in str(path or "").split("/") if segment]
    if not cleaned:
        return ""
    return "/" + "/".join(cleaned)


def _remote_dirname(path: str) -> str:
    value = _normalize_remote_path(path).rstrip("/")
    if not value or "/" not in value[1:]:
        return ""
    return value.rsplit("/", 1)[0]


def _is_video_item(item: dict) -> bool:
    if not isinstance(item, dict) or item.get("is_dir"):
        return False
    return os.path.splitext(str(item.get("name", "") or ""))[1].lower() in _VIDEO_EXTS


def _parse_tv_episode_key(name: str, path: str) -> tuple[int, int] | None:
    global _PARSE_FILENAME_FUNC
    try:
        if _PARSE_FILENAME_FUNC is None:
            from app.services.media_organize_tmdb import _parse_filename
            _PARSE_FILENAME_FUNC = _parse_filename
        parsed = _PARSE_FILENAME_FUNC(
            str(name or ""),
            media_type_hint="tv",
            file_path=str(path or ""),
            quiet=True,
        ) or {}
        season = parsed.get("season")
        episode = parsed.get("episode")
        if season is None or episode is None:
            return None
        return int(season), int(episode)
    except Exception:
        return None


def load_cache() -> dict:
    if not os.path.exists(MEDIA_LIBRARY_CACHE_FILE):
        return _default_cache()
    try:
        with open(MEDIA_LIBRARY_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_cache()
        data.setdefault("_meta", {"version": 1, "updated_at": 0})
        data.setdefault("tasks", {})
        return data
    except Exception:
        return _default_cache()


def _save_cache(data: dict):
    os.makedirs(os.path.dirname(MEDIA_LIBRARY_CACHE_FILE), exist_ok=True)
    tmp = MEDIA_LIBRARY_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, MEDIA_LIBRARY_CACHE_FILE)
    _set_cache_file_token_locked()


def _normalize_item(item: dict) -> dict:
    return {
        "name": str(item.get("name", "") or ""),
        "path": str(item.get("path", "") or ""),
        "pickcode": str(item.get("pickcode", "") or ""),
        "size": int(item.get("size", 0) or 0),
        "id": int(item.get("id", 0) or 0),
        "sha1": str(item.get("sha1", "") or ""),
        "is_dir": bool(item.get("is_dir", False)),
        "parent_id": int(item.get("parent_id", 0) or 0),
    }


def _normalize_items(items: dict) -> dict:
    normalized = {}
    for item_key, item in (items or {}).items():
        if not isinstance(item, dict):
            continue
        normalized[str(item_key)] = _normalize_item(item)
    return normalized


class MediaLibraryTaskIndex:
    def __init__(self, task_key: str, items: dict | None = None):
        self.task_key = task_key
        self.items: dict[str, dict] = {}
        self.path_to_ids: dict[str, list[str]] = {}
        self.descendants_by_path: dict[str, list[str]] = {}
        self.children_by_parent: dict[int, list[str]] = {}
        self.movie_candidates: dict[str, list[str]] = {}
        self.tv_candidates: dict[tuple[str, int, int], list[str]] = {}
        self.sha1_to_ids: dict[str, list[str]] = {}
        self.replace_items_no_lock(items or {})

    @staticmethod
    def _append(mapping: dict, key, item_id: str):
        mapping.setdefault(key, []).append(item_id)

    @staticmethod
    def _remove(mapping: dict, key, item_id: str):
        values = mapping.get(key)
        if not values:
            return
        try:
            values.remove(item_id)
        except ValueError:
            return
        if not values:
            mapping.pop(key, None)

    def _path_prefixes(self, path: str) -> list[str]:
        normalized = _normalize_remote_path(path).rstrip("/")
        if not normalized:
            return []
        parts = normalized.strip("/").split("/")
        prefixes = []
        for idx in range(1, len(parts) + 1):
            prefixes.append("/" + "/".join(parts[:idx]))
        return prefixes

    def _add_item_no_lock(self, item_key: str, item: dict):
        item_id = str(item_key or item.get("id") or "")
        if not item_id:
            return
        normalized = _normalize_item(item)
        if not normalized.get("id") and item_id.isdigit():
            normalized["id"] = int(item_id)
        self.items[item_id] = normalized

        path = _normalize_remote_path(normalized.get("path", "")).rstrip("/")
        if path:
            self._append(self.path_to_ids, path, item_id)
            for prefix in self._path_prefixes(path):
                self._append(self.descendants_by_path, prefix, item_id)

        parent_id = int(normalized.get("parent_id", 0) or 0)
        if parent_id:
            self._append(self.children_by_parent, parent_id, item_id)

        if normalized.get("is_dir"):
            return

        sha1 = str(normalized.get("sha1", "") or "").upper().strip()
        if sha1:
            self._append(self.sha1_to_ids, sha1, item_id)

        if not _is_video_item(normalized):
            return

        folder_path = _remote_dirname(normalized.get("path", ""))
        if not folder_path:
            return
        self._append(self.movie_candidates, folder_path, item_id)
        episode_key = _parse_tv_episode_key(normalized.get("name", ""), normalized.get("path", ""))
        if episode_key:
            season, episode = episode_key
            self._append(self.tv_candidates, (folder_path, season, episode), item_id)

    def _remove_item_no_lock(self, item_id: str) -> dict | None:
        item_id = str(item_id or "")
        if not item_id:
            return None
        item = self.items.pop(item_id, None)
        if not item:
            return None

        path = _normalize_remote_path(item.get("path", "")).rstrip("/")
        if path:
            self._remove(self.path_to_ids, path, item_id)
            for prefix in self._path_prefixes(path):
                self._remove(self.descendants_by_path, prefix, item_id)

        parent_id = int(item.get("parent_id", 0) or 0)
        if parent_id:
            self._remove(self.children_by_parent, parent_id, item_id)

        if item.get("is_dir"):
            return item

        sha1 = str(item.get("sha1", "") or "").upper().strip()
        if sha1:
            self._remove(self.sha1_to_ids, sha1, item_id)

        if _is_video_item(item):
            folder_path = _remote_dirname(item.get("path", ""))
            if folder_path:
                self._remove(self.movie_candidates, folder_path, item_id)
                episode_key = _parse_tv_episode_key(item.get("name", ""), item.get("path", ""))
                if episode_key:
                    season, episode = episode_key
                    self._remove(self.tv_candidates, (folder_path, season, episode), item_id)
        return item

    def replace_items_no_lock(self, items: dict):
        self.items.clear()
        self.path_to_ids.clear()
        self.descendants_by_path.clear()
        self.children_by_parent.clear()
        self.movie_candidates.clear()
        self.tv_candidates.clear()
        self.sha1_to_ids.clear()
        for item_key, item in _normalize_items(items).items():
            self._add_item_no_lock(item_key, item)

    def add_or_update_items_no_lock(self, items: dict):
        for item_key, item in _normalize_items(items).items():
            self._remove_item_no_lock(item_key)
            self._add_item_no_lock(item_key, item)

    def update_item_no_lock(self, item_id: str, item: dict):
        item_id = str(item_id or "")
        if not item_id:
            return
        self._remove_item_no_lock(item_id)
        self._add_item_no_lock(item_id, item)

    def remove_items_no_lock(self, item_ids: list[str] | set[str]) -> int:
        removed = 0
        seen = set()
        for item_id in item_ids or []:
            item_id = str(item_id or "")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            if self._remove_item_no_lock(item_id):
                removed += 1
        return removed

    def add_or_update_items(self, items: dict):
        with _lock:
            self.add_or_update_items_no_lock(items)

    def remove_items(self, item_ids: list[str] | set[str]) -> int:
        with _lock:
            return self.remove_items_no_lock(item_ids)

    def _ids_for_sha1_values_no_lock(self, sha1_values: set[str]) -> list[str]:
        result = []
        seen = set()
        for sha1 in sha1_values or set():
            normalized = str(sha1 or "").upper().strip()
            if not normalized:
                continue
            for item_id in self.sha1_to_ids.get(normalized, []):
                if item_id in seen:
                    continue
                seen.add(item_id)
                result.append(item_id)
        return result

    def ids_for_sha1_values(self, sha1_values: set[str]) -> list[str]:
        with _lock:
            return self._ids_for_sha1_values_no_lock(sha1_values)

    def _collect_descendant_ids_no_lock(self, root_item_id: str) -> list[str]:
        item = self.items.get(str(root_item_id or ""))
        if not item:
            return []
        try:
            root_parent_id = int(item.get("id", 0) or root_item_id or 0)
        except (TypeError, ValueError):
            return []
        if not root_parent_id:
            return []

        result = []
        seen = set()
        stack = [root_parent_id]
        while stack:
            parent_id = stack.pop()
            for child_id in self.children_by_parent.get(parent_id, []):
                if child_id in seen:
                    continue
                seen.add(child_id)
                result.append(child_id)
                child = self.items.get(child_id)
                if child and child.get("is_dir"):
                    try:
                        child_dir_id = int(child.get("id", 0) or child_id or 0)
                    except (TypeError, ValueError):
                        child_dir_id = 0
                    if child_dir_id:
                        stack.append(child_dir_id)
        return result

    def _ids_under_path_no_lock(self, path_prefix: str) -> list[str]:
        prefix = _normalize_remote_path(path_prefix).rstrip("/")
        if not prefix:
            return []

        result = []
        seen = set()

        def add(item_id: str):
            item_id = str(item_id or "")
            if item_id and item_id not in seen:
                seen.add(item_id)
                result.append(item_id)

        for item_id in self.descendants_by_path.get(prefix, []):
            add(item_id)
        return result

    def ids_under_path(self, path_prefix: str) -> list[str]:
        with _lock:
            return self._ids_under_path_no_lock(path_prefix)

    def find_existing_candidates(
        self,
        media_type: str,
        folder_path: str,
        season_number: int | None = None,
        episode_number: int | None = None,
    ) -> list[dict]:
        normalized_folder_path = _normalize_remote_path(folder_path)
        with _lock:
            if media_type == "tv":
                item_ids = list(self.tv_candidates.get((normalized_folder_path, season_number, episode_number), []))
            else:
                item_ids = list(self.movie_candidates.get(normalized_folder_path, []))
            return [dict(self.items[item_id]) for item_id in item_ids if item_id in self.items]

    def get_sha1_set(self) -> set[str]:
        with _lock:
            return set(self.sha1_to_ids.keys())

    def sha1_count(self) -> int:
        with _lock:
            return len(self.sha1_to_ids)

    def has_sha1(self, sha1: str) -> bool:
        normalized = str(sha1 or "").upper().strip()
        if not normalized:
            return False
        with _lock:
            return bool(self.sha1_to_ids.get(normalized))

    def item_count(self) -> int:
        with _lock:
            return len(self.items)


def get_task_index(task_key: str) -> MediaLibraryTaskIndex:
    task_key = str(task_key or "")
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        index = _resident_task_indexes.get(task_key)
        if index:
            return index
        cache = load_cache()
        tasks = cache.get("tasks", {})
        task = tasks.get(task_key, {})
        index = MediaLibraryTaskIndex(task_key, task.get("items", {}))
        _resident_task_indexes[task_key] = index
        _set_cache_file_token_locked()
        return index


def get_task_items(task_key: str) -> dict:
    cache = load_cache()
    tasks = cache.get("tasks", {})
    task = tasks.get(task_key, {})
    return dict(task.get("items", {}))


def get_task_sha1_set(task_key: str) -> set[str]:
    return get_task_index(task_key).get_sha1_set()


def _build_pickcode_index() -> dict[str, dict]:
    """遍历所有task的items，构建 pickcode → {task_key, item_key, item} 索引"""
    cache = load_cache()
    index: dict[str, dict] = {}
    for task_key, task in cache.get("tasks", {}).items():
        for item_key, item in task.get("items", {}).items():
            pc = str(item.get("pickcode", "") or "")
            if pc:
                index[pc] = {"task_key": task_key, "item_key": item_key, "item": dict(item)}
    return index


def get_item_by_pickcode(pickcode: str) -> dict | None:
    """通过 pickcode 查找缓存条目，返回 {task_key, item_key, item} 或 None"""
    global _pickcode_index, _index_dirty
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        if _pickcode_index is None or _index_dirty:
            _pickcode_index = _build_pickcode_index()
            _index_dirty = False
        return _pickcode_index.get(pickcode)


def get_item_by_id(item_id: str | int) -> dict | None:
    """通过 id 查找缓存条目，返回 {task_key, item_key, item} 或 None"""
    item_id = str(item_id or "")
    if not item_id:
        return None
    cache = load_cache()
    for task_key, task in cache.get("tasks", {}).items():
        item = (task.get("items", {}) or {}).get(item_id)
        if item:
            return {"task_key": task_key, "item_key": item_id, "item": dict(item)}
    return None


def _mark_index_dirty():
    global _index_dirty
    _index_dirty = True


def _update_task(cache: dict, task_key: str, items: dict, meta: dict | None = None, replace: bool = False):
    now = time.time()
    normalized_items = _normalize_items(items)
    tasks = cache.setdefault("tasks", {})
    task = tasks.setdefault(task_key, {})
    current_items = {} if replace else dict(task.get("items", {}))
    current_items.update(normalized_items)
    task["updated_at"] = now
    task["item_count"] = len(current_items)
    task["items"] = current_items
    if meta:
        task.update(meta)
    cache["_meta"] = {
        "version": 1,
        "updated_at": now,
    }
    return current_items, normalized_items


def merge_task_items(task_key: str, items: dict, meta: dict | None = None):
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        cache = load_cache()
        _, normalized_items = _update_task(cache, task_key, items, meta=meta, replace=False)
        _save_cache(cache)
        index = _resident_task_indexes.get(task_key)
        if index:
            index.add_or_update_items_no_lock(normalized_items)
    _mark_index_dirty()


def save_task_snapshot(task_key: str, items: dict, meta: dict | None = None, rebuild_resident_index: bool = True):
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        cache = load_cache()
        current_items, _ = _update_task(cache, task_key, items, meta=meta, replace=True)
        _save_cache(cache)
        index = _resident_task_indexes.get(task_key)
        if index:
            if rebuild_resident_index:
                index.replace_items_no_lock(current_items)
            else:
                _resident_task_indexes.pop(task_key, None)
    _mark_index_dirty()


def prune_tasks_by_keys(valid_task_keys: set[str]) -> int:
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        cache = load_cache()
        tasks = cache.setdefault("tasks", {})
        stale_keys = [task_key for task_key in list(tasks.keys()) if task_key not in valid_task_keys]
        if not stale_keys:
            return 0
        for task_key in stale_keys:
            tasks.pop(task_key, None)
            _resident_task_indexes.pop(task_key, None)
        now = time.time()
        cache["_meta"] = {
            "version": 1,
            "updated_at": now,
        }
        _save_cache(cache)
    _mark_index_dirty()
    return len(stale_keys)


def upsert_task_item(task_key: str, item_key: str, item_data: dict, meta: dict | None = None):
    merge_task_items(task_key, {str(item_key): _normalize_item(item_data)}, meta=meta)


def update_task_item_fields(
    task_key: str,
    item_id: str | int,
    *,
    name: str | None = None,
    path: str | None = None,
    meta: dict | None = None,
) -> bool:
    item_id = str(item_id or "")
    if not item_id:
        return False
    updated = False
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        cache = load_cache()
        task = cache.setdefault("tasks", {}).get(task_key)
        if not task:
            return False
        items = dict(task.get("items", {}))
        task["items"] = items
        item = items.get(item_id)
        if not item:
            return False
        if name is not None:
            item["name"] = str(name or "")
            updated = True
        if path is not None:
            item["path"] = str(path or "")
            updated = True
        if updated:
            now = time.time()
            task["updated_at"] = now
            if meta:
                task.update(meta)
            cache["_meta"] = {
                "version": 1,
                "updated_at": now,
            }
            _save_cache(cache)
            index = _resident_task_indexes.get(task_key)
            if index:
                index.update_item_no_lock(item_id, item)
    if updated:
        _mark_index_dirty()
    return updated


def remove_task_items_by_sha1(task_key: str, sha1_values: set[str], meta: dict | None = None) -> int:
    normalized_sha1s = {str(v or "").upper().strip() for v in (sha1_values or set()) if str(v or "").strip()}
    if not normalized_sha1s:
        return 0
    removed_ids = []
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        cache = load_cache()
        tasks = cache.setdefault("tasks", {})
        task = tasks.get(task_key)
        if not task:
            return 0
        items = dict(task.get("items", {}))
        index = _resident_task_indexes.get(task_key)
        if index:
            candidate_ids = index._ids_for_sha1_values_no_lock(normalized_sha1s)
            for item_id in candidate_ids:
                if item_id in items:
                    items.pop(item_id, None)
                    removed_ids.append(item_id)
        else:
            kept = {}
            for item_key, item in items.items():
                sha1 = str(item.get("sha1", "") or "").upper().strip()
                if sha1 and sha1 in normalized_sha1s:
                    removed_ids.append(item_key)
                    continue
                kept[item_key] = item
            items = kept
        removed = len(removed_ids)
        if not removed:
            return 0
        task["items"] = items
        task["item_count"] = len(items)
        now = time.time()
        task["updated_at"] = now
        if meta:
            task.update(meta)
        cache["_meta"] = {
            "version": 1,
            "updated_at": now,
        }
        _save_cache(cache)
        if index:
            index.remove_items_no_lock(removed_ids)
    _mark_index_dirty()
    return len(removed_ids)


def update_items_path_prefix(task_key: str, old_prefix: str, new_prefix: str, meta: dict | None = None) -> int:
    """更新所有 path 以 old_prefix 开头的条目，将前缀替换为 new_prefix（含文件夹条目自身）"""
    if not old_prefix or old_prefix == new_prefix:
        return 0
    old_prefix = str(old_prefix or "").rstrip("/")
    new_prefix = str(new_prefix or "").rstrip("/")
    old_prefix_norm = _normalize_remote_path(old_prefix).rstrip("/")
    if not old_prefix_norm:
        return 0
    updated_items = {}
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        cache = load_cache()
        tasks = cache.setdefault("tasks", {})
        task = tasks.get(task_key)
        if not task:
            return 0
        items = task.get("items", {})
        index = _resident_task_indexes.get(task_key)
        if index:
            candidate_ids = index._ids_under_path_no_lock(old_prefix)
            candidate_pairs = [(item_id, items.get(item_id)) for item_id in candidate_ids]
        else:
            candidate_pairs = list(items.items())
        for item_key, item in candidate_pairs:
            if not item:
                continue
            item_path = str(item.get("path", "") or "")
            item_path_norm = _normalize_remote_path(item_path).rstrip("/")
            if item_path_norm == old_prefix_norm:
                item["path"] = new_prefix
                updated_items[str(item_key)] = dict(item)
            elif item_path_norm.startswith(old_prefix_norm + "/"):
                item["path"] = new_prefix + item_path_norm[len(old_prefix_norm):]
                updated_items[str(item_key)] = dict(item)
        if not updated_items:
            return 0
        now = time.time()
        task["updated_at"] = now
        if meta:
            task.update(meta)
        cache["_meta"] = {
            "version": 1,
            "updated_at": now,
        }
        _save_cache(cache)
        if index:
            for item_key, item in updated_items.items():
                index.update_item_no_lock(item_key, item)
    _mark_index_dirty()
    return len(updated_items)


def get_dir_by_parent_and_name(task_key: str, parent_id: int, name: str) -> tuple[int, str] | None:
    """查找 parent_id 下名为 name 的目录，返回 (id, pickcode) 或 None"""
    items = get_task_items(task_key)
    for item in items.values():
        if item.get("is_dir") and item.get("parent_id") == parent_id and item.get("name") == name:
            return int(item.get("id", 0)), str(item.get("pickcode", "") or "")
    return None


def get_dir_by_name(task_key: str, name: str) -> tuple[int, str] | None:
    """按 name 查找目录条目（不限 parent_id），返回 (id, pickcode) 或 None"""
    items = get_task_items(task_key)
    for item in items.values():
        if item.get("is_dir") and item.get("name") == name:
            return int(item.get("id", 0)), str(item.get("pickcode", "") or "")
    return None


def get_dir_by_path(task_key: str, path: str) -> tuple[int, str] | None:
    """按完整 path 查找目录条目，返回 (id, pickcode) 或 None"""
    normalized_path = _normalize_remote_path(path).rstrip("/")
    if not normalized_path:
        return None
    items = get_task_items(task_key)
    for item in items.values():
        item_path = _normalize_remote_path(str(item.get("path", "") or "")).rstrip("/")
        if item.get("is_dir") and item_path == normalized_path:
            return int(item.get("id", 0)), str(item.get("pickcode", "") or "")
    return None


def upsert_dir_item(task_key: str, item_id: int, name: str, parent_id: int, pickcode: str = "", path: str = ""):
    """写入或更新一个目录条目到媒体库缓存"""
    upsert_task_item(task_key, str(item_id), {
        "id": item_id,
        "name": name,
        "parent_id": parent_id,
        "pickcode": pickcode,
        "path": path,
        "is_dir": True,
        "size": 0,
        "sha1": "",
    })


def remove_items_by_path_prefix(task_key: str, path_prefix: str, meta: dict | None = None) -> int:
    """删除 path 等于 path_prefix 或以 path_prefix/ 开头的所有条目（含文件夹自身）"""
    if not path_prefix:
        return 0
    path_prefix = str(path_prefix or "").rstrip("/")
    path_prefix_norm = _normalize_remote_path(path_prefix).rstrip("/")
    if not path_prefix_norm:
        return 0
    removed_ids = []
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        cache = load_cache()
        tasks = cache.setdefault("tasks", {})
        task = tasks.get(task_key)
        if not task:
            return 0
        items = dict(task.get("items", {}))
        index = _resident_task_indexes.get(task_key)
        if index:
            candidate_ids = index._ids_under_path_no_lock(path_prefix)
            for item_id in candidate_ids:
                if item_id in items:
                    items.pop(item_id, None)
                    removed_ids.append(item_id)
        else:
            kept = {}
            for item_key, item in items.items():
                item_path = _normalize_remote_path(str(item.get("path", "") or "")).rstrip("/")
                if item_path == path_prefix_norm or item_path.startswith(path_prefix_norm + "/"):
                    removed_ids.append(item_key)
                    continue
                kept[item_key] = item
            items = kept
        if not removed_ids:
            return 0
        task["items"] = items
        task["item_count"] = len(items)
        now = time.time()
        task["updated_at"] = now
        if meta:
            task.update(meta)
        cache["_meta"] = {
            "version": 1,
            "updated_at": now,
        }
        _save_cache(cache)
        if index:
            index.remove_items_no_lock(removed_ids)
    _mark_index_dirty()
    return len(removed_ids)


def get_task_item_by_id(task_key: str, item_id: str | int) -> dict | None:
    """按 id 查找 task 内单条缓存条目"""
    item_id = str(item_id or "")
    if not item_id:
        return None
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        index = _resident_task_indexes.get(task_key)
        if index and item_id in index.items:
            return dict(index.items[item_id])
    items = get_task_items(task_key)
    item = items.get(item_id)
    return dict(item) if item else None


def remove_task_item_by_id(task_key: str, item_id: str | int, meta: dict | None = None) -> int:
    """按 id 删除单条缓存条目"""
    item_id = str(item_id or "")
    if not item_id:
        return 0
    removed = 0
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        cache = load_cache()
        task = cache.setdefault("tasks", {}).get(task_key)
        if not task:
            return 0
        items = dict(task.get("items", {}))
        if item_id in items:
            items.pop(item_id, None)
            removed = 1
        if removed:
            task["items"] = items
            task["item_count"] = len(items)
            now = time.time()
            task["updated_at"] = now
            if meta:
                task.update(meta)
            cache["_meta"] = {"version": 1, "updated_at": now}
            _save_cache(cache)
            index = _resident_task_indexes.get(task_key)
            if index:
                index.remove_items_no_lock([item_id])
    if removed:
        _mark_index_dirty()
    return removed


def remove_task_item_by_pickcode(task_key: str, pickcode: str, meta: dict | None = None) -> int:
    """按 pickcode 删除单条缓存条目"""
    if not pickcode:
        return 0
    removed_ids = []
    with _lock:
        _drop_resident_indexes_if_file_changed_locked()
        cache = load_cache()
        task = cache.setdefault("tasks", {}).get(task_key)
        if not task:
            return 0
        items = dict(task.get("items", {}))
        kept = {}
        for item_key, item in items.items():
            if str(item.get("pickcode", "") or "") == pickcode:
                removed_ids.append(item_key)
                continue
            kept[item_key] = item
        if removed_ids:
            task["items"] = kept
            task["item_count"] = len(kept)
            now = time.time()
            task["updated_at"] = now
            if meta:
                task.update(meta)
            cache["_meta"] = {"version": 1, "updated_at": now}
            _save_cache(cache)
            index = _resident_task_indexes.get(task_key)
            if index:
                index.remove_items_no_lock(removed_ids)
    if removed_ids:
        _mark_index_dirty()
    return len(removed_ids)
