"""Status enums and helpers for queue items and worker lifecycle.

Centralises all status strings that were previously magic strings scattered
across worker.py, app.py and the frontend. The ``.value`` of each enum
member MUST match the string the frontend CSS expects (``.status-pending``
etc.) and the JSON SSE payload — changing a value is a breaking change.
"""

from __future__ import annotations

from enum import Enum


class SongStatus(str, Enum):
    """Lifecycle status of a single queue item.

    Values are lower-case strings identical to the old magic strings,
    so existing frontend CSS classes and SSE consumers work unchanged.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"

    @classmethod
    def from_value(cls, value: str) -> "SongStatus | None":
        """Tolerant lookup: accept the enum value, name, or return None."""
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            return None

    @classmethod
    def is_terminal(cls, value) -> bool:
        """True if the status is a final state (done or error)."""
        s = cls.from_value(value)
        return s in (cls.DONE, cls.ERROR)

    @classmethod
    def is_active(cls, value) -> bool:
        """True if the status is pending or processing (not yet final)."""
        s = cls.from_value(value)
        return s in (cls.PENDING, cls.PROCESSING)


class WorkerState(str, Enum):
    """High-level worker lifecycle state (distinct from per-item status)."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"


# Conveniences for SSE payloads — keeps the broadcast call sites readable.
def status_payload(item_id: int, status: SongStatus, **extra) -> dict:
    """Build a standard SSE status message dict.

    Usage::

        sse_broadcast("status", status_payload(42, SongStatus.PROCESSING,
                                               progress="Lade Cover ..."))
    """
    payload = {"id": item_id, "status": status.value}
    payload.update(extra)
    return payload
