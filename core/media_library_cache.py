from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterable

from core.cache_db import get_cache_db_file
from core.configs import MEDIA_LIBRARY_CACHE_FILE

try:
    from core.logger import logger
except Exception:  # pragma: no cover - logger is available in normal runtime
    import logging
    logger = logging.getLogger("MediaLibraryCache")


_lock = threading.RLock()
_init_lock = threading.RLock()
_initialized = False
_warmup_thread: threading.Thread | None = None

MEDIA_LIBRARY_CACHE_DB_FILE = get_cache_db_file()

_SCHEMA_VERSION = 1
_SQLITE_VARIABLE_LIMIT = 900

_VIDEO_EXTS = {
    '.mp4', '.mpg', '.mkv', '.mpeg', '.ts', '.vob', '.iso', '.m4v', '.avi', '.3gp', '.wmv', '.webm',
    '.flv', '.mov', '.m2ts', '.rmvb', '.rm', '.asf', '.f4v', '.m2t', '.mts', '.mpe', '.tp', '.trp',
    '.divx', '.ogv', '.dv'
}
_PARSE_FILENAME_FUNC = None


def build_task_key(drive_index: int, remote_path: str) -> str:
    return f"{drive_index}:{str(remote_path or '').rstrip('/')}"


def _default_cache() -> dict:
    return {
        "_meta": {
            "version": 1,
            "updated_at": 0,
        },
        "tasks": {},
    }


def _safe_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


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


def _normalize_item(item: dict) -> dict:
    item = item or {}
    return {
        "name": str(item.get("name", "") or ""),
        "path": str(item.get("path", "") or ""),
        "pickcode": str(item.get("pickcode", "") or ""),
        "size": _safe_int(item.get("size", 0), 0),
        "id": _safe_int(item.get("id", 0), 0),
        "sha1": str(item.get("sha1", "") or ""),
        "is_dir": bool(item.get("is_dir", False)),
        "parent_id": _safe_int(item.get("parent_id", 0), 0),
    }


def _normalize_items(items: dict) -> dict:
    normalized = {}
    for item_key, item in (items or {}).items():
        if not isinstance(item, dict):
            continue
        normalized[str(item_key)] = _normalize_item(item)
    return normalized


def _json_cache_signature() -> str:
    try:
        stat = os.stat(MEDIA_LIBRARY_CACHE_FILE)
        return f"{int(stat.st_mtime_ns)}:{int(stat.st_size)}"
    except OSError:
        return ""


def _open_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(MEDIA_LIBRARY_CACHE_DB_FILE), exist_ok=True)
    conn = sqlite3.connect(MEDIA_LIBRARY_CACHE_DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


@contextmanager
def _db(write: bool = False):
    _ensure_initialized()
    conn = _open_connection()
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cache_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            task_key TEXT PRIMARY KEY,
            updated_at REAL NOT NULL DEFAULT 0,
            item_count INTEGER NOT NULL DEFAULT 0,
            meta_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS media_items (
            task_key TEXT NOT NULL,
            item_key TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            path_norm TEXT NOT NULL DEFAULT '',
            pickcode TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            id INTEGER NOT NULL DEFAULT 0,
            sha1 TEXT NOT NULL DEFAULT '',
            sha1_norm TEXT NOT NULL DEFAULT '',
            is_dir INTEGER NOT NULL DEFAULT 0,
            parent_id INTEGER NOT NULL DEFAULT 0,
            media_kind TEXT NOT NULL DEFAULT '',
            folder_path_norm TEXT NOT NULL DEFAULT '',
            season INTEGER,
            episode INTEGER,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (task_key, item_key)
        );

        CREATE INDEX IF NOT EXISTS idx_media_items_task_sha1
            ON media_items(task_key, sha1_norm);
        CREATE INDEX IF NOT EXISTS idx_media_items_pickcode
            ON media_items(pickcode);
        CREATE INDEX IF NOT EXISTS idx_media_items_id
            ON media_items(id);
        CREATE INDEX IF NOT EXISTS idx_media_items_task_id
            ON media_items(task_key, id);
        CREATE INDEX IF NOT EXISTS idx_media_items_task_parent_name_dir
            ON media_items(task_key, parent_id, name, is_dir);
        CREATE INDEX IF NOT EXISTS idx_media_items_task_path_dir
            ON media_items(task_key, path_norm, is_dir);
        CREATE INDEX IF NOT EXISTS idx_media_items_task_path
            ON media_items(task_key, path_norm);
        CREATE INDEX IF NOT EXISTS idx_media_items_task_folder_kind
            ON media_items(task_key, folder_path_norm, media_kind, is_dir);
        CREATE INDEX IF NOT EXISTS idx_media_items_task_tv_candidate
            ON media_items(task_key, folder_path_norm, season, episode, media_kind, is_dir);
        """
    )
    _set_meta(conn, "schema_version", str(_SCHEMA_VERSION))


def _get_meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM cache_meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(key), str(value)),
    )


def _load_json_cache_file() -> dict:
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
    except Exception as e:
        logger.warning(f"[MediaLibraryCache] JSON 缓存读取失败，跳过迁移: {e}")
        return _default_cache()


def _db_item_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM media_items").fetchone()
    return int(row["count"] or 0) if row else 0


def _task_item_count(conn: sqlite3.Connection, task_key: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM media_items WHERE task_key = ?",
        (str(task_key or ""),),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def _task_meta(conn: sqlite3.Connection, task_key: str) -> dict:
    row = conn.execute(
        "SELECT updated_at, item_count, meta_json FROM tasks WHERE task_key = ?",
        (str(task_key or ""),),
    ).fetchone()
    if not row:
        return {}
    try:
        meta = json.loads(row["meta_json"] or "{}")
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    meta.setdefault("updated_at", float(row["updated_at"] or 0))
    meta.setdefault("item_count", int(row["item_count"] or 0))
    return meta


def _set_task_meta(
    conn: sqlite3.Connection,
    task_key: str,
    meta: dict | None = None,
    *,
    item_count: int | None = None,
    replace_meta: bool = False,
) -> dict:
    task_key = str(task_key or "")
    existing = {} if replace_meta else _task_meta(conn, task_key)
    now = time.time()
    payload = dict(existing)
    payload["updated_at"] = now
    if item_count is None:
        item_count = _task_item_count(conn, task_key)
    payload["item_count"] = int(item_count or 0)
    if meta:
        payload.update(meta)
    updated_at = float(payload.get("updated_at") or now)
    conn.execute(
        "INSERT INTO tasks(task_key, updated_at, item_count, meta_json) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(task_key) DO UPDATE SET "
        "updated_at = excluded.updated_at, item_count = excluded.item_count, meta_json = excluded.meta_json",
        (task_key, updated_at, int(item_count or 0), json.dumps(payload, ensure_ascii=False)),
    )
    return payload


def _row_to_item(row: sqlite3.Row | dict) -> dict:
    return {
        "name": str(row["name"] or ""),
        "path": str(row["path"] or ""),
        "pickcode": str(row["pickcode"] or ""),
        "size": int(row["size"] or 0),
        "id": int(row["id"] or 0),
        "sha1": str(row["sha1"] or ""),
        "is_dir": bool(row["is_dir"]),
        "parent_id": int(row["parent_id"] or 0),
    }


def _row_to_cache_entry(row: sqlite3.Row) -> dict:
    item_key = str(row["item_key"] or row["id"] or "")
    return {
        "task_key": str(row["task_key"] or ""),
        "item_key": item_key,
        "item": _row_to_item(row),
    }


def _item_to_row(task_key: str, item_key: str, item: dict, updated_at: float | None = None) -> tuple:
    normalized = _normalize_item(item)
    item_key = str(item_key or normalized.get("id") or "")
    if not normalized.get("id") and item_key.isdigit():
        normalized["id"] = int(item_key)
    path_norm = _normalize_remote_path(normalized.get("path", "")).rstrip("/")
    sha1_norm = str(normalized.get("sha1", "") or "").upper().strip()
    media_kind = "video" if _is_video_item(normalized) else ""
    folder_path_norm = _remote_dirname(path_norm) if media_kind == "video" else ""
    season = item.get("season") if isinstance(item, dict) else None
    episode = item.get("episode") if isinstance(item, dict) else None
    season = _safe_int(season, None) if season is not None else None
    episode = _safe_int(episode, None) if episode is not None else None
    return (
        str(task_key or ""),
        item_key,
        normalized["name"],
        normalized["path"],
        path_norm,
        normalized["pickcode"],
        normalized["size"],
        normalized["id"],
        normalized["sha1"],
        sha1_norm,
        1 if normalized["is_dir"] else 0,
        normalized["parent_id"],
        media_kind,
        folder_path_norm,
        season,
        episode,
        float(updated_at or time.time()),
    )


def _upsert_items(conn: sqlite3.Connection, task_key: str, items: dict) -> dict:
    normalized_items = _normalize_items(items)
    if not normalized_items:
        return {}
    updated_at = time.time()
    rows = [
        _item_to_row(task_key, item_key, item, updated_at=updated_at)
        for item_key, item in normalized_items.items()
        if str(item_key or "")
    ]
    conn.executemany(
        """
        INSERT INTO media_items(
            task_key, item_key, name, path, path_norm, pickcode, size, id, sha1, sha1_norm,
            is_dir, parent_id, media_kind, folder_path_norm, season, episode, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_key, item_key) DO UPDATE SET
            name = excluded.name,
            path = excluded.path,
            path_norm = excluded.path_norm,
            pickcode = excluded.pickcode,
            size = excluded.size,
            id = excluded.id,
            sha1 = excluded.sha1,
            sha1_norm = excluded.sha1_norm,
            is_dir = excluded.is_dir,
            parent_id = excluded.parent_id,
            media_kind = excluded.media_kind,
            folder_path_norm = excluded.folder_path_norm,
            season = COALESCE(excluded.season, media_items.season),
            episode = COALESCE(excluded.episode, media_items.episode),
            updated_at = excluded.updated_at
        """,
        rows,
    )
    return normalized_items


def _replace_task_items(conn: sqlite3.Connection, task_key: str, items: dict, meta: dict | None = None) -> None:
    task_key = str(task_key or "")
    conn.execute("DELETE FROM media_items WHERE task_key = ?", (task_key,))
    normalized_items = _upsert_items(conn, task_key, items)
    _set_task_meta(
        conn,
        task_key,
        meta=meta,
        item_count=len(normalized_items),
        replace_meta=True,
    )


def _migrate_json_cache_if_needed(conn: sqlite3.Connection) -> None:
    signature = _json_cache_signature()
    if not signature:
        return
    if _db_item_count(conn) > 0:
        return
    data = _load_json_cache_file()
    tasks = data.get("tasks") if isinstance(data, dict) else {}
    if not isinstance(tasks, dict) or not tasks:
        return

    started = time.time()
    total_items = 0
    logger.info(f"[MediaLibraryCache] 开始迁移 JSON 媒体库缓存到 SQLite: {MEDIA_LIBRARY_CACHE_DB_FILE}")
    for task_key, task in tasks.items():
        if not isinstance(task, dict):
            continue
        items = task.get("items", {}) if isinstance(task.get("items", {}), dict) else {}
        meta = {k: v for k, v in task.items() if k != "items"}
        _replace_task_items(conn, str(task_key), items, meta=meta)
        total_items += len(items)
    _set_meta(conn, "json_migrated_signature", signature)
    _set_meta(conn, "json_migrated_at", str(time.time()))
    logger.info(
        f"[MediaLibraryCache] SQLite 迁移完成: tasks={len(tasks)} items={total_items} "
        f"耗时 {time.time() - started:.1f}s"
    )


def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        with _open_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _create_schema(conn)
                _migrate_json_cache_if_needed(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        _initialized = True


def _chunked(values: Iterable, chunk_size: int = _SQLITE_VARIABLE_LIMIT):
    chunk = []
    for value in values:
        chunk.append(value)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _path_prefix_bounds(path_prefix: str) -> tuple[str, str, str]:
    prefix = _normalize_remote_path(path_prefix).rstrip("/")
    return prefix, prefix + "/", prefix + "0"


def load_cache() -> dict:
    """Return a JSON-compatible snapshot. Prefer targeted helpers for hot paths."""
    with _db() as conn:
        result = _default_cache()
        meta_row = conn.execute("SELECT MAX(updated_at) AS updated_at FROM tasks").fetchone()
        result["_meta"]["updated_at"] = float(meta_row["updated_at"] or 0) if meta_row else 0
        task_rows = conn.execute("SELECT task_key, updated_at, item_count, meta_json FROM tasks").fetchall()
        for task_row in task_rows:
            task_key = str(task_row["task_key"] or "")
            try:
                task_payload = json.loads(task_row["meta_json"] or "{}")
                if not isinstance(task_payload, dict):
                    task_payload = {}
            except Exception:
                task_payload = {}
            task_payload.setdefault("updated_at", float(task_row["updated_at"] or 0))
            task_payload.setdefault("item_count", int(task_row["item_count"] or 0))
            task_payload["items"] = {}
            for item_row in conn.execute(
                "SELECT * FROM media_items WHERE task_key = ?",
                (task_key,),
            ):
                task_payload["items"][str(item_row["item_key"] or "")] = _row_to_item(item_row)
            result["tasks"][task_key] = task_payload
        return result


def _save_cache(data: dict):
    """Compatibility helper: replace the SQLite cache from a JSON-shaped payload."""
    if not isinstance(data, dict):
        return
    with _lock, _db(write=True) as conn:
        conn.execute("DELETE FROM media_items")
        conn.execute("DELETE FROM tasks")
        tasks = data.get("tasks", {})
        for task_key, task in (tasks or {}).items():
            if not isinstance(task, dict):
                continue
            meta = {k: v for k, v in task.items() if k != "items"}
            _replace_task_items(conn, str(task_key), task.get("items", {}) or {}, meta=meta)
        _set_meta(conn, "compat_save_at", str(time.time()))


class MediaLibraryTaskIndex:
    """SQLite-backed task index facade.

    The old implementation built large resident Python indexes from JSON.  This
    facade keeps the public API but resolves lookups through SQLite indexes.
    """

    def __init__(self, task_key: str, items: dict | None = None):
        self.task_key = str(task_key or "")
        if items:
            with _lock, _db(write=True) as conn:
                _replace_task_items(conn, self.task_key, items)

    def find_existing_candidates(
        self,
        media_type: str,
        folder_path: str,
        season_number: int | None = None,
        episode_number: int | None = None,
    ) -> list[dict]:
        normalized_folder_path = _normalize_remote_path(folder_path).rstrip("/")
        if not normalized_folder_path:
            return []
        with _db() as conn:
            if media_type == "tv" and season_number is not None and episode_number is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM media_items
                    WHERE task_key = ? AND is_dir = 0 AND media_kind = 'video'
                      AND folder_path_norm = ? AND season = ? AND episode = ?
                    """,
                    (self.task_key, normalized_folder_path, int(season_number), int(episode_number)),
                ).fetchall()
                if rows:
                    return [_row_to_item(row) for row in rows]

                rows = conn.execute(
                    """
                    SELECT * FROM media_items
                    WHERE task_key = ? AND is_dir = 0 AND media_kind = 'video'
                      AND folder_path_norm = ?
                    """,
                    (self.task_key, normalized_folder_path),
                ).fetchall()
                matches = []
                parsed_updates = []
                for row in rows:
                    row_season = row["season"]
                    row_episode = row["episode"]
                    if row_season is None or row_episode is None:
                        parsed = _parse_tv_episode_key(row["name"], row["path"])
                        if parsed:
                            row_season, row_episode = parsed
                            parsed_updates.append((int(row_season), int(row_episode), row["task_key"], row["item_key"]))
                    if row_season == int(season_number) and row_episode == int(episode_number):
                        matches.append(_row_to_item(row))
                if parsed_updates:
                    with _lock, _db(write=True) as write_conn:
                        write_conn.executemany(
                            "UPDATE media_items SET season = ?, episode = ? WHERE task_key = ? AND item_key = ?",
                            parsed_updates,
                        )
                return matches

            rows = conn.execute(
                """
                SELECT * FROM media_items
                WHERE task_key = ? AND is_dir = 0 AND media_kind = 'video'
                  AND folder_path_norm = ?
                """,
                (self.task_key, normalized_folder_path),
            ).fetchall()
            return [_row_to_item(row) for row in rows]

    def get_sha1_set(self) -> set[str]:
        with _db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT sha1_norm FROM media_items WHERE task_key = ? AND sha1_norm != ''",
                (self.task_key,),
            ).fetchall()
            return {str(row["sha1_norm"] or "") for row in rows if row["sha1_norm"]}

    def sha1_count(self) -> int:
        with _db() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT sha1_norm) AS count FROM media_items WHERE task_key = ? AND sha1_norm != ''",
                (self.task_key,),
            ).fetchone()
            return int(row["count"] or 0) if row else 0

    def has_sha1(self, sha1: str) -> bool:
        normalized = str(sha1 or "").upper().strip()
        if not normalized:
            return False
        with _db() as conn:
            row = conn.execute(
                "SELECT 1 FROM media_items WHERE task_key = ? AND sha1_norm = ? LIMIT 1",
                (self.task_key, normalized),
            ).fetchone()
            return row is not None

    def first_item_for_sha1(self, sha1: str) -> dict | None:
        normalized = str(sha1 or "").upper().strip()
        if not normalized:
            return None
        with _db() as conn:
            row = conn.execute(
                """
                SELECT * FROM media_items
                WHERE task_key = ? AND sha1_norm = ? AND is_dir = 0
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (self.task_key, normalized),
            ).fetchone()
            return _row_to_item(row) if row else None

    def item_count(self) -> int:
        with _db() as conn:
            return _task_item_count(conn, self.task_key)

    def ids_for_sha1_values(self, sha1_values: set[str]) -> list[str]:
        normalized_sha1s = [str(v or "").upper().strip() for v in (sha1_values or set()) if str(v or "").strip()]
        if not normalized_sha1s:
            return []
        result = []
        with _db() as conn:
            for chunk in _chunked(normalized_sha1s):
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT item_key FROM media_items WHERE task_key = ? AND sha1_norm IN ({placeholders})",
                    (self.task_key, *chunk),
                ).fetchall()
                result.extend(str(row["item_key"] or "") for row in rows)
        return result

    def ids_under_path(self, path_prefix: str) -> list[str]:
        prefix, lower, upper = _path_prefix_bounds(path_prefix)
        if not prefix:
            return []
        with _db() as conn:
            rows = conn.execute(
                """
                SELECT item_key FROM media_items
                WHERE task_key = ? AND (path_norm = ? OR (path_norm >= ? AND path_norm < ?))
                """,
                (self.task_key, prefix, lower, upper),
            ).fetchall()
            return [str(row["item_key"] or "") for row in rows]

    def add_or_update_items(self, items: dict):
        merge_task_items(self.task_key, items)

    def remove_items(self, item_ids: list[str] | set[str]) -> int:
        removed = 0
        ids = [str(v or "") for v in (item_ids or []) if str(v or "")]
        if not ids:
            return 0
        with _lock, _db(write=True) as conn:
            for chunk in _chunked(ids):
                placeholders = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    f"DELETE FROM media_items WHERE task_key = ? AND item_key IN ({placeholders})",
                    (self.task_key, *chunk),
                )
                removed += int(cur.rowcount or 0)
            if removed:
                _set_task_meta(conn, self.task_key)
        return removed


def get_task_index(task_key: str) -> MediaLibraryTaskIndex:
    return MediaLibraryTaskIndex(str(task_key or ""))


def get_task_items(task_key: str) -> dict:
    task_key = str(task_key or "")
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM media_items WHERE task_key = ?",
            (task_key,),
        ).fetchall()
        return {str(row["item_key"] or ""): _row_to_item(row) for row in rows}


def get_task_sha1_set(task_key: str) -> set[str]:
    return get_task_index(task_key).get_sha1_set()


def get_item_by_pickcode(pickcode: str) -> dict | None:
    pickcode = str(pickcode or "")
    if not pickcode:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM media_items WHERE pickcode = ? LIMIT 1",
            (pickcode,),
        ).fetchone()
        return _row_to_cache_entry(row) if row else None


def get_item_by_id(item_id: str | int) -> dict | None:
    item_id = str(item_id or "")
    if not item_id:
        return None
    with _db() as conn:
        if item_id.isdigit():
            row = conn.execute(
                "SELECT * FROM media_items WHERE item_key = ? OR id = ? LIMIT 1",
                (item_id, int(item_id)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM media_items WHERE item_key = ? LIMIT 1",
                (item_id,),
            ).fetchone()
        return _row_to_cache_entry(row) if row else None


def _mark_index_dirty():
    return None


def merge_task_items(task_key: str, items: dict, meta: dict | None = None):
    task_key = str(task_key or "")
    if not task_key or not items:
        return
    with _lock, _db(write=True) as conn:
        _upsert_items(conn, task_key, items)
        _set_task_meta(conn, task_key, meta=meta)


def save_task_snapshot(task_key: str, items: dict, meta: dict | None = None, rebuild_resident_index: bool = True):
    task_key = str(task_key or "")
    with _lock, _db(write=True) as conn:
        _replace_task_items(conn, task_key, items or {}, meta=meta)


def prune_tasks_by_keys(valid_task_keys: set[str]) -> int:
    valid = {str(v or "") for v in (valid_task_keys or set()) if str(v or "")}
    with _lock, _db(write=True) as conn:
        rows = conn.execute("SELECT task_key FROM tasks").fetchall()
        stale_keys = [str(row["task_key"] or "") for row in rows if str(row["task_key"] or "") not in valid]
        for task_key in stale_keys:
            conn.execute("DELETE FROM media_items WHERE task_key = ?", (task_key,))
            conn.execute("DELETE FROM tasks WHERE task_key = ?", (task_key,))
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
    task_key = str(task_key or "")
    item_id = str(item_id or "")
    if not task_key or not item_id:
        return False
    with _lock, _db(write=True) as conn:
        row = conn.execute(
            "SELECT * FROM media_items WHERE task_key = ? AND item_key = ?",
            (task_key, item_id),
        ).fetchone()
        if not row:
            return False
        item = _row_to_item(row)
        updated = False
        if name is not None:
            item["name"] = str(name or "")
            updated = True
        if path is not None:
            item["path"] = str(path or "")
            updated = True
        if not updated:
            return False
        _upsert_items(conn, task_key, {item_id: item})
        _set_task_meta(conn, task_key, meta=meta)
        return True


def remove_task_items_by_sha1(task_key: str, sha1_values: set[str], meta: dict | None = None) -> int:
    task_key = str(task_key or "")
    normalized_sha1s = [str(v or "").upper().strip() for v in (sha1_values or set()) if str(v or "").strip()]
    if not task_key or not normalized_sha1s:
        return 0
    removed = 0
    with _lock, _db(write=True) as conn:
        for chunk in _chunked(normalized_sha1s):
            placeholders = ",".join("?" for _ in chunk)
            cur = conn.execute(
                f"DELETE FROM media_items WHERE task_key = ? AND sha1_norm IN ({placeholders})",
                (task_key, *chunk),
            )
            removed += int(cur.rowcount or 0)
        if removed:
            _set_task_meta(conn, task_key, meta=meta)
    return removed


def update_items_path_prefix(task_key: str, old_prefix: str, new_prefix: str, meta: dict | None = None) -> int:
    if not old_prefix or old_prefix == new_prefix:
        return 0
    task_key = str(task_key or "")
    new_prefix = str(new_prefix or "").rstrip("/")
    old_prefix_norm, lower, upper = _path_prefix_bounds(str(old_prefix or "").rstrip("/"))
    if not task_key or not old_prefix_norm:
        return 0

    with _lock, _db(write=True) as conn:
        rows = conn.execute(
            """
            SELECT * FROM media_items
            WHERE task_key = ? AND (path_norm = ? OR (path_norm >= ? AND path_norm < ?))
            """,
            (task_key, old_prefix_norm, lower, upper),
        ).fetchall()
        if not rows:
            return 0
        updated_items = {}
        for row in rows:
            item = _row_to_item(row)
            item_path_norm = _normalize_remote_path(item.get("path", "")).rstrip("/")
            suffix = ""
            if item_path_norm == old_prefix_norm:
                suffix = ""
            elif item_path_norm.startswith(old_prefix_norm + "/"):
                suffix = item_path_norm[len(old_prefix_norm):]
            else:
                continue
            item["path"] = new_prefix + suffix
            updated_items[str(row["item_key"] or "")] = item
        if not updated_items:
            return 0
        _upsert_items(conn, task_key, updated_items)
        _set_task_meta(conn, task_key, meta=meta)
        return len(updated_items)


def get_dir_by_parent_and_name(task_key: str, parent_id: int, name: str) -> tuple[int, str] | None:
    with _db() as conn:
        row = conn.execute(
            """
            SELECT id, pickcode FROM media_items
            WHERE task_key = ? AND parent_id = ? AND name = ? AND is_dir = 1
            LIMIT 1
            """,
            (str(task_key or ""), int(parent_id or 0), str(name or "")),
        ).fetchone()
        if row:
            return int(row["id"] or 0), str(row["pickcode"] or "")
    return None


def get_dir_by_name(task_key: str, name: str) -> tuple[int, str] | None:
    with _db() as conn:
        row = conn.execute(
            """
            SELECT id, pickcode FROM media_items
            WHERE task_key = ? AND name = ? AND is_dir = 1
            LIMIT 1
            """,
            (str(task_key or ""), str(name or "")),
        ).fetchone()
        if row:
            return int(row["id"] or 0), str(row["pickcode"] or "")
    return None


def get_dir_by_path(task_key: str, path: str) -> tuple[int, str] | None:
    normalized_path = _normalize_remote_path(path).rstrip("/")
    if not normalized_path:
        return None
    with _db() as conn:
        row = conn.execute(
            """
            SELECT id, pickcode FROM media_items
            WHERE task_key = ? AND path_norm = ? AND is_dir = 1
            LIMIT 1
            """,
            (str(task_key or ""), normalized_path),
        ).fetchone()
        if row:
            return int(row["id"] or 0), str(row["pickcode"] or "")
    return None


def upsert_dir_item(task_key: str, item_id: int, name: str, parent_id: int, pickcode: str = "", path: str = ""):
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
    task_key = str(task_key or "")
    prefix, lower, upper = _path_prefix_bounds(path_prefix)
    if not task_key or not prefix:
        return 0
    with _lock, _db(write=True) as conn:
        cur = conn.execute(
            """
            DELETE FROM media_items
            WHERE task_key = ? AND (path_norm = ? OR (path_norm >= ? AND path_norm < ?))
            """,
            (task_key, prefix, lower, upper),
        )
        removed = int(cur.rowcount or 0)
        if removed:
            _set_task_meta(conn, task_key, meta=meta)
        return removed


def get_task_item_by_id(task_key: str, item_id: str | int) -> dict | None:
    task_key = str(task_key or "")
    item_id = str(item_id or "")
    if not task_key or not item_id:
        return None
    with _db() as conn:
        if item_id.isdigit():
            row = conn.execute(
                """
                SELECT * FROM media_items
                WHERE task_key = ? AND (item_key = ? OR id = ?)
                LIMIT 1
                """,
                (task_key, item_id, int(item_id)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM media_items WHERE task_key = ? AND item_key = ? LIMIT 1",
                (task_key, item_id),
            ).fetchone()
        return _row_to_item(row) if row else None


def get_task_item_by_path(task_key: str, path: str) -> dict | None:
    task_key = str(task_key or "")
    normalized_path = _normalize_remote_path(path).rstrip("/")
    if not task_key or not normalized_path:
        return None
    with _db() as conn:
        row = conn.execute(
            """
            SELECT * FROM media_items
            WHERE task_key = ? AND path_norm = ?
            LIMIT 1
            """,
            (task_key, normalized_path),
        ).fetchone()
        return _row_to_item(row) if row else None


def remove_task_item_by_id(task_key: str, item_id: str | int, meta: dict | None = None) -> int:
    task_key = str(task_key or "")
    item_id = str(item_id or "")
    if not task_key or not item_id:
        return 0
    with _lock, _db(write=True) as conn:
        if item_id.isdigit():
            cur = conn.execute(
                "DELETE FROM media_items WHERE task_key = ? AND (item_key = ? OR id = ?)",
                (task_key, item_id, int(item_id)),
            )
        else:
            cur = conn.execute(
                "DELETE FROM media_items WHERE task_key = ? AND item_key = ?",
                (task_key, item_id),
            )
        removed = int(cur.rowcount or 0)
        if removed:
            _set_task_meta(conn, task_key, meta=meta)
        return removed


def remove_task_item_by_pickcode(task_key: str, pickcode: str, meta: dict | None = None) -> int:
    task_key = str(task_key or "")
    pickcode = str(pickcode or "")
    if not task_key or not pickcode:
        return 0
    with _lock, _db(write=True) as conn:
        cur = conn.execute(
            "DELETE FROM media_items WHERE task_key = ? AND pickcode = ?",
            (task_key, pickcode),
        )
        removed = int(cur.rowcount or 0)
        if removed:
            _set_task_meta(conn, task_key, meta=meta)
        return removed


def warmup_cache_in_background() -> None:
    """Initialize/migrate the SQLite cache on a daemon thread after app startup."""
    global _warmup_thread
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        if _warmup_thread and _warmup_thread.is_alive():
            return

        def _worker():
            try:
                started = time.time()
                _ensure_initialized()
                with _open_connection() as conn:
                    item_count = _db_item_count(conn)
                logger.info(
                    f"[MediaLibraryCache] SQLite 缓存预热完成: items={item_count} "
                    f"耗时 {time.time() - started:.1f}s"
                )
            except Exception as e:
                logger.warning(f"[MediaLibraryCache] SQLite 缓存预热失败: {e}")

        _warmup_thread = threading.Thread(target=_worker, name="media-library-cache-warmup", daemon=True)
        _warmup_thread.start()
