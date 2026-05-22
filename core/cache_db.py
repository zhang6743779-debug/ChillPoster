from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

from core.configs import APP_CACHE_DB_FILE, MEDIA_LIBRARY_CACHE_DB_FILE, TMDB_CACHE_DB_FILE


APP_CACHE_DB = os.getenv("CHILLPOSTER_CACHE_DB") or APP_CACHE_DB_FILE
MEDIA_LIBRARY_CACHE_DB = os.getenv("CHILLPOSTER_MEDIA_LIBRARY_CACHE_DB") or MEDIA_LIBRARY_CACHE_DB_FILE
TMDB_CACHE_DB = os.getenv("CHILLPOSTER_TMDB_CACHE_DB") or TMDB_CACHE_DB_FILE


def get_cache_db_file() -> str:
    return MEDIA_LIBRARY_CACHE_DB


def _open_connection(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def open_cache_connection() -> sqlite3.Connection:
    return _open_connection(APP_CACHE_DB)


def open_tmdb_cache_connection() -> sqlite3.Connection:
    return _open_connection(TMDB_CACHE_DB)


@contextmanager
def cache_db(write: bool = False):
    conn = open_cache_connection()
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


@contextmanager
def tmdb_cache_db(write: bool = False):
    conn = open_tmdb_cache_connection()
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
