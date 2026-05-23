import json
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from core.configs import CONFIG_DIR

HISTORY_FILE = os.path.join(CONFIG_DIR, "organize_history.json")
MAX_HISTORY_RECORDS = 3000
_LOCK = threading.RLock()


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


def _save_records_unlocked(records: list[dict]) -> None:
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp_path = f"{HISTORY_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fp:
        json.dump(records[:MAX_HISTORY_RECORDS], fp, ensure_ascii=False, indent=2)
    os.replace(tmp_path, HISTORY_FILE)


def append_organize_history(record: dict) -> dict:
    now = time.time()
    item = {
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
    with _LOCK:
        records = _load_records_unlocked()
        records.insert(0, item)
        _save_records_unlocked(records)
    return item


def list_organize_history() -> list[dict]:
    with _LOCK:
        return list(_load_records_unlocked())
