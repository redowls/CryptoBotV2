"""Heartbeat liveness (Phase 7, SUMMARY Observability).

The daemon writes a ``heartbeat`` row every ``obs.heartbeat_interval_secs`` and
once per trade cycle. An external monitor (:mod:`scripts.monitor_heartbeat`)
reads the latest row and ALERTS when it goes stale — a dead bot must never
silently leave open positions unmanaged.

Writes are BEST-EFFORT: observability must never break the trade path, so the
daemon wraps these in try/except. The pure staleness helpers
(:func:`seconds_since`, :func:`is_stale`) are side-effect-free for the offline
green-light.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from core import config
from core.db import connect_with_retry

# Allowed status values (mirrors the CK_heartbeat_status CHECK constraint).
ALIVE = "alive"        # daemon liveness tick
OK = "ok"              # cycle completed clean
DEGRADED = "degraded"  # cycle completed with per-symbol error(s)
ERROR = "error"        # cycle crashed


@dataclass(frozen=True)
class HeartbeatParams:
    interval_secs: int
    stale_secs: int

    @classmethod
    def from_config(cls) -> "HeartbeatParams":
        return cls(
            interval_secs=config.get_int("obs.heartbeat_interval_secs", 300),
            stale_secs=config.get_int("obs.heartbeat_stale_secs", 900),
        )


# --------------------------------------------------------------------------
# Pure helpers (offline-testable)
# --------------------------------------------------------------------------
def seconds_since(last_ts: datetime, now: Optional[datetime] = None) -> float:
    """Seconds elapsed between a (naive-UTC or aware) timestamp and now."""
    if now is None:
        now = datetime.now(timezone.utc)
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - last_ts).total_seconds()


def is_stale(last_ts: Optional[datetime], threshold_secs: float, now: Optional[datetime] = None) -> bool:
    """True if there is no heartbeat, or the last one is older than threshold."""
    if last_ts is None:
        return True
    return seconds_since(last_ts, now) > threshold_secs


# --------------------------------------------------------------------------
# DB I/O
# --------------------------------------------------------------------------
def open_position_count(conn: Connection) -> int:
    """Count of positions currently flagged open (for the alert context)."""
    row = conn.execute(
        text("SELECT COUNT(*) FROM dbo.positions WHERE status = 'open'")
    ).fetchone()
    return int(row[0]) if row else 0


def write_heartbeat(
    *,
    component: str,
    status: str,
    detail: Optional[str] = None,
    open_positions: Optional[int] = None,
    equity: Optional[float] = None,
) -> int:
    """Insert one heartbeat row; returns the new id.

    Opens its own short transaction. The caller (daemon) wraps this best-effort
    so a DB blip surfaces as a 'DB unreachable' alert rather than a crash.
    """
    with connect_with_retry() as conn:
        with conn.begin():
            if open_positions is None:
                open_positions = open_position_count(conn)
            row = conn.execute(
                text(
                    "INSERT INTO dbo.heartbeat "
                    "  (component, status, detail, open_positions, equity, host) "
                    "OUTPUT INSERTED.id "
                    "VALUES (:component, :status, :detail, :open_positions, :equity, :host)"
                ),
                {
                    "component": component,
                    "status": status,
                    "detail": detail,
                    "open_positions": open_positions,
                    "equity": equity,
                    "host": socket.gethostname()[:128],
                },
            ).fetchone()
    return int(row[0])


def latest_heartbeat(component: Optional[str] = None) -> Optional[dict]:
    """Return the most recent heartbeat (optionally for one component) as a dict.

    Keys: id, component, status, detail, open_positions, equity, host,
    created_at_utc. None if the table is empty.
    """
    sql = (
        "SELECT TOP 1 id, component, status, detail, open_positions, equity, host, "
        "       created_at_utc "
        "FROM dbo.heartbeat "
    )
    params: dict = {}
    if component is not None:
        sql += "WHERE component = :component "
        params["component"] = component
    sql += "ORDER BY created_at_utc DESC"

    with connect_with_retry() as conn:
        row = conn.execute(text(sql), params).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "component": row[1],
        "status": row[2],
        "detail": row[3],
        "open_positions": row[4],
        "equity": float(row[5]) if row[5] is not None else None,
        "host": row[6],
        "created_at_utc": row[7],
    }
