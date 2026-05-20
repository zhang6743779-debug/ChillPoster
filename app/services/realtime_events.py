"""Lightweight in-process event fanout for UI cache updates."""
from __future__ import annotations

import json
import queue
import threading
import time
from contextlib import contextmanager
from typing import Iterator


_subscribers: set[queue.Queue] = set()
_lock = threading.RLock()


@contextmanager
def subscribe_realtime_events() -> Iterator[queue.Queue]:
    q: queue.Queue = queue.Queue(maxsize=100)
    with _lock:
        _subscribers.add(q)
    try:
        yield q
    finally:
        with _lock:
            _subscribers.discard(q)


def publish_realtime_event(event_type: str, payload: dict | None = None) -> None:
    event = {
        "type": str(event_type or "message"),
        "payload": payload or {},
        "ts": int(time.time()),
    }
    with _lock:
        subscribers = list(_subscribers)
    for q in subscribers:
        try:
            q.put_nowait(event)
        except queue.Full:
            try:
                q.get_nowait()
                q.put_nowait(event)
            except Exception:
                pass


def format_sse_event(event: dict) -> str:
    event_type = str(event.get("type") or "message")
    data = json.dumps(event.get("payload") or {}, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {data}\n\n"
