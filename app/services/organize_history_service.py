import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from core.cache_db import cache_db
from core.configs import CONFIG_DIR

HISTORY_FILE = os.path.join(CONFIG_DIR, "organize_history.json")
DEFAULT_HISTORY_QUERY_LIMIT = 3000
_LOCK = threading.RLock()
_DB_READY = False
logger = logging.getLogger("ChillPoster.organize_history")

HISTORY_COLUMNS = (
    "id",
    "created_at",
    "timestamp",
    "category",
    "status",
    "title",
    "year",
    "season_episode",
    "media_type",
    "tmdb_id",
    "source_file",
    "target_file",
    "source_path",
    "target_path",
    "library_location",
    "quality",
    "video",
    "audio",
    "size",
    "reason",
    "decision",
    "summary",
)

SEARCH_COLUMNS = (
    "title",
    "year",
    "season_episode",
    "media_type",
    "tmdb_id",
    "source_file",
    "target_file",
    "source_path",
    "target_path",
    "library_location",
    "quality",
    "video",
    "audio",
    "size",
    "reason",
    "decision",
    "summary",
)


def _safe_text(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _load_records_unlocked() -> list[dict]:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _normalize_record(record: dict) -> dict:
    now = time.time()
    return {
        "id": record.get("id") or f"{int(now * 1000)}-{uuid.uuid4().hex[:8]}",
        "created_at": record.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": float(record.get("timestamp") or now),
        "category": _safe_text(record.get("category") or "organize_success", 64),
        "status": _safe_text(record.get("status") or "", 32),
        "title": _safe_text(record.get("title") or record.get("source_file") or "整理记录", 140),
        "year": _safe_text(record.get("year") or "", 16),
        "season_episode": _safe_text(record.get("season_episode") or "", 64),
        "media_type": _safe_text(record.get("media_type") or "", 16),
        "tmdb_id": _safe_text(record.get("tmdb_id") or "", 32),
        "source_file": _safe_text(record.get("source_file") or "", 220),
        "target_file": _safe_text(record.get("target_file") or "", 220),
        "source_path": _safe_text(record.get("source_path") or "", 360),
        "target_path": _safe_text(record.get("target_path") or "", 360),
        "library_location": _safe_text(record.get("library_location") or "", 260),
        "quality": _safe_text(record.get("quality") or "", 120),
        "video": _safe_text(record.get("video") or "", 120),
        "audio": _safe_text(record.get("audio") or "", 120),
        "size": _safe_text(record.get("size") or "", 32),
        "reason": _safe_text(record.get("reason") or "", 180),
        "decision": _safe_text(record.get("decision") or "", 180),
        "summary": _safe_text(record.get("summary") or "", 260),
    }


def _create_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS organize_history (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT '',
            timestamp REAL NOT NULL DEFAULT 0,
            category TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            year TEXT NOT NULL DEFAULT '',
            season_episode TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT '',
            tmdb_id TEXT NOT NULL DEFAULT '',
            source_file TEXT NOT NULL DEFAULT '',
            target_file TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL DEFAULT '',
            target_path TEXT NOT NULL DEFAULT '',
            library_location TEXT NOT NULL DEFAULT '',
            quality TEXT NOT NULL DEFAULT '',
            video TEXT NOT NULL DEFAULT '',
            audio TEXT NOT NULL DEFAULT '',
            size TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_organize_history_category_timestamp
            ON organize_history(category, timestamp DESC);

        CREATE INDEX IF NOT EXISTS idx_organize_history_timestamp
            ON organize_history(timestamp DESC);
        """
    )


def _insert_record_unlocked(conn, item: dict) -> None:
    placeholders = ", ".join("?" for _ in HISTORY_COLUMNS)
    columns = ", ".join(HISTORY_COLUMNS)
    updates = ", ".join(f"{column}=excluded.{column}" for column in HISTORY_COLUMNS if column != "id")
    conn.execute(
        f"""
        INSERT INTO organize_history({columns})
        VALUES({placeholders})
        ON CONFLICT(id) DO UPDATE SET {updates}
        """,
        tuple(item.get(column, "") for column in HISTORY_COLUMNS),
    )


def _insert_record_ignore_unlocked(conn, item: dict) -> bool:
    placeholders = ", ".join("?" for _ in HISTORY_COLUMNS)
    columns = ", ".join(HISTORY_COLUMNS)
    cursor = conn.execute(
        f"""
        INSERT OR IGNORE INTO organize_history({columns})
        VALUES({placeholders})
        """,
        tuple(item.get(column, "") for column in HISTORY_COLUMNS),
    )
    return cursor.rowcount > 0


def _normalize_legacy_record(record: dict) -> dict:
    item = _normalize_record(record)
    if record.get("id"):
        return item
    fingerprint = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    item["id"] = f"legacy-{hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:16]}"
    return item


def _migrate_json_history_unlocked(conn) -> None:
    records = _load_records_unlocked()
    if not records:
        return
    migrated = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        if _insert_record_ignore_unlocked(conn, _normalize_legacy_record(record)):
            migrated += 1
    if migrated:
        logger.info(f"[OrganizeHistory] 已合并整理记录 JSON 到 SQLite: {migrated} 条")


def _ensure_db_ready() -> None:
    global _DB_READY
    if _DB_READY:
        return
    with _LOCK:
        if _DB_READY:
            return
        with cache_db(write=True) as conn:
            _create_schema(conn)
            _migrate_json_history_unlocked(conn)
        _DB_READY = True


def _row_to_record(row) -> dict:
    return {column: row[column] for column in HISTORY_COLUMNS}


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_where_clause(category: str = "", keyword: str = "") -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    category = (category or "").strip()
    keyword = (keyword or "").strip()
    if category:
        clauses.append("category = ?")
        params.append(category)
    if keyword:
        pattern = f"%{_escape_like(keyword)}%"
        keyword_clause = " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in SEARCH_COLUMNS)
        clauses.append(f"({keyword_clause})")
        params.extend(pattern for _ in SEARCH_COLUMNS)
    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def append_organize_history(record: dict) -> dict:
    item = _normalize_record(record)
    with _LOCK:
        _ensure_db_ready()
        with cache_db(write=True) as conn:
            _insert_record_unlocked(conn, item)
    return item


def count_organize_history_by_category(keyword: str = "") -> dict[str, int]:
    with _LOCK:
        _ensure_db_ready()
        where_clause, params = _build_where_clause(keyword=keyword)
        with cache_db() as conn:
            rows = conn.execute(
                f"""
                SELECT category, COUNT(*) AS count
                FROM organize_history
                {where_clause}
                GROUP BY category
                """,
                params,
            ).fetchall()
        return {str(row["category"] or ""): int(row["count"] or 0) for row in rows}


def list_organize_history_page(
    *,
    category: str = "",
    keyword: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    limit = max(1, int(limit or 50))
    offset = max(0, int(offset or 0))
    with _LOCK:
        _ensure_db_ready()
        where_clause, params = _build_where_clause(category=category, keyword=keyword)
        with cache_db() as conn:
            rows = conn.execute(
                f"""
                SELECT id, created_at, timestamp, category, status, title, year,
                       season_episode, media_type, tmdb_id, source_file, target_file,
                       source_path, target_path, library_location, quality, video,
                       audio, size, reason, decision, summary
                FROM organize_history
                {where_clause}
                ORDER BY timestamp DESC, rowid DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return [_row_to_record(row) for row in rows]


def list_organize_history(limit: int = DEFAULT_HISTORY_QUERY_LIMIT) -> list[dict]:
    return list_organize_history_page(limit=limit)
