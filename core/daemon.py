"""Long-running daemon (Phase 7, SUMMARY Observability + cadence §12).

A single persistent process that does two things on independent schedules:

  * fires the hourly trade cycle at ``obs.cycle_minute`` past each hour
    (a small offset so the just-closed 1h bar has settled on Alpaca first); and
  * writes a liveness heartbeat every ``obs.heartbeat_interval_secs`` so a HUNG
    cycle is detectable BETWEEN hours, not just at the top of the next one.

Implemented on the stdlib (a computed-next-fire sleep loop) rather than a new
scheduler dependency — the same minimal-dep / lazy-import ethos as the rest of
the bot. The scheduling math (:func:`next_cycle_time`) is a pure helper the
offline green-light pins.

Critical alerts raised here (SUMMARY): a cycle CRASH (escalated when positions
are open), an Alpaca AUTH failure, and the DB being UNREACHABLE for a heartbeat
write. Alerting is fail-safe (never raises), so a broken alert path degrades to
a log line and the loop keeps running.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from core import alerting, config, heartbeat, obs
from core.db import connect_with_retry
from core.orchestrator import run_cycle

_log = obs.get_logger("daemon")


# --------------------------------------------------------------------------
# Pure helpers (offline-testable)
# --------------------------------------------------------------------------
def next_cycle_time(now: datetime, cycle_minute: int) -> datetime:
    """Next ``HH:cycle_minute:00`` strictly after ``now`` (UTC)."""
    candidate = now.replace(minute=cycle_minute % 60, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(hours=1)
    return candidate


def looks_like_auth_error(exc: BaseException) -> bool:
    """Heuristic: does this exception look like a broker AUTH failure?

    Matches on status code / wording rather than importing Alpaca's exception
    classes, so it stays robust across SDK versions.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        token in text
        for token in ("401", "403", "unauthorized", "forbidden", "invalid api", "authentication")
    )


def looks_like_db_error(exc: BaseException) -> bool:
    """Heuristic: does this exception look like the DB being unreachable?"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        token in text
        for token in ("operationalerror", "interfaceerror", "pyodbc", "tcp provider", "login timeout", "server is not found")
    )


# --------------------------------------------------------------------------
# Loop
# --------------------------------------------------------------------------
def _do_heartbeat() -> None:
    """Best-effort liveness tick; a DB failure here is a critical alert."""
    try:
        hb_id = heartbeat.write_heartbeat(component="daemon", status=heartbeat.ALIVE)
        _log.info("heartbeat", id=hb_id, status=heartbeat.ALIVE)
    except Exception as exc:
        _log.error("heartbeat_failed", error=str(exc))
        alerting.send_alert(
            "DB unreachable",
            f"Heartbeat write failed: {type(exc).__name__}: {exc}",
            severity=alerting.CRITICAL,
        )


def _do_cycle(dry_run: bool) -> None:
    """Run one trade cycle; classify + alert on crashes, then heartbeat the result."""
    try:
        rc = run_cycle(dry_run=dry_run)
        status = heartbeat.OK if rc == 0 else heartbeat.DEGRADED
        _log.info("cycle_done", rc=rc, status=status)
        try:
            heartbeat.write_heartbeat(component="cycle", status=status, detail=f"rc={rc}")
        except Exception as exc:
            _log.error("cycle_heartbeat_failed", error=str(exc))
        if rc != 0:
            alerting.send_alert(
                "Trade cycle degraded",
                "One or more symbols errored this cycle (rc=1). See logs.",
                severity=alerting.WARNING,
            )
    except Exception as exc:
        # A genuine crash. Escalate hard when positions are open and unmanaged.
        open_n: Optional[int] = None
        try:
            with connect_with_retry() as conn:
                open_n = heartbeat.open_position_count(conn)
        except Exception:
            open_n = None

        _log.critical("cycle_crash", error=str(exc), open_positions=open_n)
        try:
            heartbeat.write_heartbeat(
                component="cycle", status=heartbeat.ERROR,
                detail=f"{type(exc).__name__}: {exc}", open_positions=open_n,
            )
        except Exception:
            pass

        if looks_like_auth_error(exc):
            alerting.send_alert(
                "Alpaca auth failure",
                f"Trade cycle failed authentication: {type(exc).__name__}: {exc}",
                severity=alerting.CRITICAL,
            )
        elif looks_like_db_error(exc):
            alerting.send_alert(
                "DB unreachable",
                f"Trade cycle could not reach the database: {type(exc).__name__}: {exc}",
                severity=alerting.CRITICAL,
            )
        else:
            pos = "UNKNOWN" if open_n is None else str(open_n)
            sev = alerting.CRITICAL if (open_n is None or open_n > 0) else alerting.WARNING
            alerting.send_alert(
                "Trade cycle crashed",
                f"Unhandled error with {pos} open position(s): "
                f"{type(exc).__name__}: {exc}",
                severity=sev,
            )


def run_forever(*, dry_run: bool = False, max_iterations: Optional[int] = None) -> int:
    """Run the daemon loop. ``max_iterations`` bounds it for testing (None = forever)."""
    obs.configure()
    hb = heartbeat.HeartbeatParams.from_config()
    cycle_minute = config.get_int("obs.cycle_minute", 1)

    _log.info(
        "daemon_start", dry_run=dry_run,
        heartbeat_interval_secs=hb.interval_secs, cycle_minute=cycle_minute,
    )

    now = datetime.now(timezone.utc)
    next_hb = now  # write one immediately so the monitor sees life at once
    next_cycle = next_cycle_time(now, cycle_minute)

    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        now = datetime.now(timezone.utc)
        if now >= next_hb:
            _do_heartbeat()
            next_hb = now + timedelta(seconds=hb.interval_secs)
        if now >= next_cycle:
            _do_cycle(dry_run)
            next_cycle = next_cycle_time(now, cycle_minute)

        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            break

        wake = min(next_hb, next_cycle)
        sleep_secs = max(1.0, (wake - datetime.now(timezone.utc)).total_seconds())
        time.sleep(sleep_secs)

    return 0
