r"""Phase 7 — external heartbeat monitor (the independent watchdog).

Reads the latest dbo.heartbeat row and, if it is older than obs.heartbeat_stale_secs,
sends a Telegram alert: the daemon is hung or dead. The alert is escalated to
CRITICAL when the last heartbeat shows open positions (a dead bot leaving money
unmanaged is the exact failure this whole phase exists to catch).

Run this as a SEPARATE process from the daemon — a cron job / systemd timer on
the VPS, or healthchecks.io hitting an endpoint — so it can still fire when the
daemon process itself is wedged. Exit code 0 = healthy, 1 = stale/no heartbeat.

Usage:
    .\.venv\Scripts\python.exe -m scripts.monitor_heartbeat
    # cron (VPS), every 5 min:
    #   */5 * * * * cd /opt/tradebot && .venv/bin/python -m scripts.monitor_heartbeat
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from core import alerting, heartbeat, obs

_log = obs.get_logger("monitor")


def main(argv: list[str]) -> int:
    load_dotenv()
    hb = heartbeat.HeartbeatParams.from_config()

    try:
        latest = heartbeat.latest_heartbeat()
    except Exception as exc:
        # Can't even read the heartbeat table — the DB itself is a problem.
        _log.critical("monitor_db_error", error=str(exc))
        alerting.send_alert(
            "DB unreachable (monitor)",
            f"Heartbeat monitor could not query the DB: {type(exc).__name__}: {exc}",
            severity=alerting.CRITICAL,
        )
        print(f"DB ERROR: {type(exc).__name__}: {exc}")
        return 1

    last_ts = latest["created_at_utc"] if latest else None
    stale = heartbeat.is_stale(last_ts, hb.stale_secs)

    if not stale:
        age = heartbeat.seconds_since(last_ts)
        _log.info("monitor_ok", age_secs=round(age, 1), component=latest["component"])
        print(
            f"OK  last heartbeat {age:,.0f}s ago "
            f"(component={latest['component']} status={latest['status']} "
            f"open={latest['open_positions']})"
        )
        return 0

    open_n = latest["open_positions"] if latest else None
    age_txt = "never" if last_ts is None else f"{heartbeat.seconds_since(last_ts):,.0f}s ago"
    severity = alerting.CRITICAL if (open_n is None or open_n > 0) else alerting.WARNING
    body = (
        f"No heartbeat within {hb.stale_secs}s (last: {age_txt}). "
        f"Open positions: {'UNKNOWN' if open_n is None else open_n}. "
        f"The daemon may be hung or dead."
    )
    _log.critical("monitor_stale", age=age_txt, open_positions=open_n, threshold_secs=hb.stale_secs)
    alerting.send_alert("Heartbeat stalled", body, severity=severity)
    # Record the staleness detection itself as a heartbeat row (component='monitor').
    try:
        heartbeat.write_heartbeat(
            component="monitor", status=heartbeat.ERROR, detail=body, open_positions=open_n
        )
    except Exception:
        pass
    print(f"STALE  {body}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
