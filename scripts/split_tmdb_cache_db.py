#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.configs import APP_CACHE_DB_FILE, BACKUPS_DIR, TMDB_CACHE_DB_FILE  # noqa: E402


TMDB_TABLES = ("tmdb_detail_cache", "tmdb_search_cache")


def quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def table_exists(conn: sqlite3.Connection, schema: str, table: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {quote_ident(schema)}.sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return bool(row)


def row_count(conn: sqlite3.Connection, schema: str, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(schema)}.{quote_ident(table)}").fetchone()[0] or 0)


def table_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA {quote_ident(schema)}.table_info({quote_ident(table)})").fetchall()
    return [str(row[1]) for row in rows]


def create_table_from_main_schema(conn: sqlite3.Connection, table: str) -> None:
    row = conn.execute(
        "SELECT sql FROM main.sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"Missing source schema for {table}")
    source_sql = str(row[0])
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\"%s\"|%s)" % (re.escape(table), re.escape(table)),
        re.IGNORECASE,
    )
    target_sql = pattern.sub(f"CREATE TABLE IF NOT EXISTS tmdb_cache.{quote_ident(table)}", source_sql, count=1)
    conn.execute(target_sql)


def create_indexes_from_main_schema(conn: sqlite3.Connection, table: str) -> None:
    rows = conn.execute(
        """
        SELECT name, sql
        FROM main.sqlite_master
        WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL
        """,
        (table,),
    ).fetchall()
    for name, sql in rows:
        name = str(name)
        source_sql = str(sql)
        pattern = re.compile(
            r"CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\"%s\"|%s)\s+ON\s+(?:\"%s\"|%s)"
            % (re.escape(name), re.escape(name), re.escape(table), re.escape(table)),
            re.IGNORECASE,
        )

        def replace(match: re.Match) -> str:
            unique = match.group(1) or ""
            return f"CREATE {unique}INDEX IF NOT EXISTS tmdb_cache.{quote_ident(name)} ON {quote_ident(table)}"

        target_sql = pattern.sub(replace, source_sql, count=1)
        conn.execute(target_sql)


def copy_table(conn: sqlite3.Connection, table: str) -> tuple[int, int]:
    create_table_from_main_schema(conn, table)
    source_cols = table_columns(conn, "main", table)
    target_cols = set(table_columns(conn, "tmdb_cache", table))
    columns = [col for col in source_cols if col in target_cols]
    if not columns:
        raise RuntimeError(f"No compatible columns for {table}")
    col_sql = ", ".join(quote_ident(col) for col in columns)
    source_count = row_count(conn, "main", table)
    conn.execute(
        f"INSERT OR REPLACE INTO tmdb_cache.{quote_ident(table)} ({col_sql}) "
        f"SELECT {col_sql} FROM main.{quote_ident(table)}"
    )
    target_count = row_count(conn, "tmdb_cache", table)
    create_indexes_from_main_schema(conn, table)
    return source_count, target_count


def backup_database(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{source.stem}.before_tmdb_split.{stamp}.db"
    source_conn = sqlite3.connect(str(source))
    backup_conn = sqlite3.connect(str(backup_path))
    try:
        source_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source_conn.backup(backup_conn)
    finally:
        backup_conn.close()
        source_conn.close()
    return backup_path


def migrate(source: Path, dest: Path, backup_dir: Path, vacuum: bool = True) -> None:
    if not source.exists():
        print(f"source database does not exist: {source}")
        return

    backup_path = backup_database(source, backup_dir)
    print(f"backup: {backup_path}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(source), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("ATTACH DATABASE ? AS tmdb_cache", (str(dest),))
        moved_tables: list[str] = []
        for table in TMDB_TABLES:
            if not table_exists(conn, "main", table):
                continue
            source_count, target_count = copy_table(conn, table)
            if target_count < source_count:
                raise RuntimeError(f"copy verification failed for {table}: source={source_count}, target={target_count}")
            moved_tables.append(table)
            print(f"copied {table}: {source_count} rows")

        if not moved_tables:
            print("no TMDB cache tables found in app cache database")
        else:
            for table in moved_tables:
                conn.execute(f"DROP TABLE IF EXISTS main.{quote_ident(table)}")
            conn.commit()
            print(f"dropped from app cache db: {', '.join(moved_tables)}")

        conn.execute("DETACH DATABASE tmdb_cache")
        conn.close()
        conn = None

        if vacuum:
            vacuum_conn = sqlite3.connect(str(source), timeout=60)
            try:
                vacuum_conn.execute("PRAGMA busy_timeout=60000")
                vacuum_conn.execute("VACUUM")
                vacuum_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                vacuum_conn.close()
            print("vacuum: complete")
    finally:
        if conn is not None:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Move TMDB caches out of app_cache.db.")
    parser.add_argument("--source", default=APP_CACHE_DB_FILE)
    parser.add_argument("--dest", default=TMDB_CACHE_DB_FILE)
    parser.add_argument("--backup-dir", default=BACKUPS_DIR)
    parser.add_argument("--skip-vacuum", action="store_true")
    args = parser.parse_args()
    migrate(Path(args.source), Path(args.dest), Path(args.backup_dir), vacuum=not args.skip_vacuum)


if __name__ == "__main__":
    main()
