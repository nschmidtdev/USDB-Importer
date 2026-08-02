"""Shared application state, SSE broadcast, and queue snapshot.

Lives at a low level (no imports from app.py) so that worker.py, youtube.py,
and songs.py can depend on it without creating circular imports.
"""

import json
import queue
import threading

from config import load_config


class State:
    def __init__(self):
        self.config = load_config()
        self.queue = []
        self.queue_lock = threading.Lock()
        self.cache_lock = threading.Lock()
        # Worker lifecycle lock: makes start/stop atomic (Task 3)
        self.worker_lock = threading.Lock()
        # Monotonic counter so queue IDs are never reused after deletion
        self.queue_next_id = 1
        self.sse_clients = []
        self.sse_lock = threading.Lock()
        self.worker_running = False
        self.worker_thread = None
        self.worker_generation = 0
        self.worker_stop_event = threading.Event()
        self.login_transfer_token = None
        self.local_songs = []
        self.usdb_cache = {}


# Singleton instance shared across the entire application.
state = State()


# === SSE ===

# Sentinel used to terminate an evicted SSE generator.
SSE_CLOSE = object()


def sse_broadcast(msg_type, data):
    msg = json.dumps({"type": msg_type, "data": data}, ensure_ascii=False)
    dead = []
    with state.sse_lock:
        for i, q in enumerate(state.sse_clients):
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(i)
                # Make room for a close sentinel so the request thread exits;
                # merely removing the queue from the registry leaves its
                # generator blocked forever.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(SSE_CLOSE)
                except queue.Full:
                    pass
        for i in reversed(dead):
            state.sse_clients.pop(i)


# === Queue snapshot ===

def get_queue_snapshot():
    with state.queue_lock:
        return [dict(item) for item in state.queue]
