import json
import os
import sqlite3
import threading
import time
from typing import Callable

from core.configs import CONFIG_DIR
from core.logger import logger


WEBHOOK_QUEUE_DB_FILE = os.path.join(CONFIG_DIR, "webhook_queue.db")
WEBHOOK_QUEUE_MAX_ATTEMPTS = 5
WEBHOOK_QUEUE_DONE_RETENTION_SECONDS = 7 * 86400

_worker = None
_worker_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(WEBHOOK_QUEUE_DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_webhook_queue():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'pending',
                event_type TEXT NOT NULL DEFAULT '',
                item_id TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_run_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                last_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_webhook_jobs_pick "
            "ON webhook_jobs(status, next_run_at, id)"
        )
        conn.execute(
            "UPDATE webhook_jobs SET status='pending', updated_at=? "
            "WHERE status='processing'",
            (time.time(),),
        )


def enqueue_webhook_payload(payload: dict, event_type: str = "", item_id: str = "") -> int:
    init_webhook_queue()
    now = time.time()
    payload_text = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO webhook_jobs
                (status, event_type, item_id, payload, attempts, next_run_at, created_at, updated_at)
            VALUES
                ('pending', ?, ?, ?, 0, 0, ?, ?)
            """,
            (str(event_type or ""), str(item_id or ""), payload_text, now, now),
        )
        return int(cur.lastrowid)


def get_webhook_queue_stats() -> dict:
    init_webhook_queue()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM webhook_jobs GROUP BY status"
        ).fetchall()
        latest = conn.execute(
            "SELECT id, status, event_type, item_id, attempts, created_at, updated_at, last_error "
            "FROM webhook_jobs ORDER BY id DESC LIMIT 10"
        ).fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    return {
        "counts": counts,
        "worker_running": is_webhook_queue_worker_running(),
        "latest": [dict(row) for row in latest],
    }


def _mark_done(job_id: int, result: dict | None = None):
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE webhook_jobs
            SET status='done', updated_at=?, completed_at=?, last_error=?
            WHERE id=?
            """,
            (now, now, "", job_id),
        )


def _mark_failed_or_retry(job_id: int, attempts: int, error: Exception):
    now = time.time()
    error_text = str(error)[:2000]
    if attempts >= WEBHOOK_QUEUE_MAX_ATTEMPTS:
        status = "failed"
        next_run_at = 0
        logger.error(f"[WebhookQueue] 任务失败并停止重试: job={job_id} attempts={attempts} error={error_text}")
    else:
        status = "pending"
        next_run_at = now + min(300, 2 ** max(0, attempts - 1))
        logger.warning(f"[WebhookQueue] 任务失败，稍后重试: job={job_id} attempts={attempts} error={error_text}")
    with _connect() as conn:
        conn.execute(
            """
            UPDATE webhook_jobs
            SET status=?, next_run_at=?, updated_at=?, last_error=?
            WHERE id=?
            """,
            (status, next_run_at, now, error_text, job_id),
        )


def _cleanup_done_jobs():
    cutoff = time.time() - WEBHOOK_QUEUE_DONE_RETENTION_SECONDS
    with _connect() as conn:
        conn.execute(
            "DELETE FROM webhook_jobs WHERE status='done' AND completed_at IS NOT NULL AND completed_at < ?",
            (cutoff,),
        )


class WebhookQueueWorker:
    def __init__(self, handler: Callable[[dict], dict | None], poll_interval: float = 0.5):
        self.handler = handler
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="webhook-queue-worker", daemon=True)

    def start(self):
        init_webhook_queue()
        self.thread.start()
        logger.info("[WebhookQueue] 持久化队列 worker 已启动")

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        logger.info("[WebhookQueue] 持久化队列 worker 已停止")

    def _claim_job(self):
        now = time.time()
        with _connect() as conn:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT id, payload, attempts
                    FROM webhook_jobs
                    WHERE status='pending' AND next_run_at <= ?
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if not row:
                    conn.execute("COMMIT")
                    return None
                attempts = int(row["attempts"]) + 1
                conn.execute(
                    """
                    UPDATE webhook_jobs
                    SET status='processing', attempts=?, updated_at=?
                    WHERE id=?
                    """,
                    (attempts, now, int(row["id"])),
                )
                conn.execute("COMMIT")
                return {
                    "id": int(row["id"]),
                    "payload": str(row["payload"] or "{}"),
                    "attempts": attempts,
                }
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _run(self):
        last_cleanup_at = 0.0
        while not self.stop_event.is_set():
            try:
                if time.time() - last_cleanup_at > 3600:
                    _cleanup_done_jobs()
                    last_cleanup_at = time.time()

                job = self._claim_job()
                if not job:
                    self.stop_event.wait(self.poll_interval)
                    continue

                try:
                    payload = json.loads(job["payload"])
                    result = self.handler(payload)
                    if isinstance(result, dict) and result.get("status") == "error":
                        raise RuntimeError(result.get("reason") or "Webhook processing returned error")
                    _mark_done(job["id"], result if isinstance(result, dict) else None)
                except Exception as e:
                    _mark_failed_or_retry(job["id"], job["attempts"], e)
            except Exception as e:
                logger.error(f"[WebhookQueue] worker 循环异常: {e}", exc_info=True)
                self.stop_event.wait(2)


def start_webhook_queue_worker(handler: Callable[[dict], dict | None]):
    global _worker
    with _worker_lock:
        if _worker and _worker.thread.is_alive():
            return
        _worker = WebhookQueueWorker(handler)
        _worker.start()


def stop_webhook_queue_worker():
    global _worker
    with _worker_lock:
        worker = _worker
        _worker = None
    if worker:
        worker.stop()


def is_webhook_queue_worker_running() -> bool:
    with _worker_lock:
        return bool(_worker and _worker.thread.is_alive())
