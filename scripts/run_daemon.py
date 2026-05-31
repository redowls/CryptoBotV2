r"""Phase 7 — the long-running daemon entrypoint.

Runs the trade cycle hourly (at obs.cycle_minute past the hour) AND writes a
liveness heartbeat every obs.heartbeat_interval_secs, in one persistent process.
Logs structured JSON to obs.log_dir (daily-rotated) + stderr. Critical faults
(cycle crash with open positions, Alpaca auth failure, DB unreachable) raise a
Telegram alert.

This is the production run mode. On the VPS, supervise it with systemd so it
restarts on exit; the external monitor (scripts.monitor_heartbeat) is the
independent watchdog that catches the case where THIS process is hung/dead.

Usage:
    .\.venv\Scripts\python.exe -m scripts.run_daemon            # live cadence
    .\.venv\Scripts\python.exe -m scripts.run_daemon --dry-run  # cycles size+report, NO orders
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from core.daemon import run_forever


def main(argv: list[str]) -> int:
    load_dotenv()
    dry_run = "--dry-run" in argv
    try:
        return run_forever(dry_run=dry_run)
    except KeyboardInterrupt:
        print("\nDaemon stopped (KeyboardInterrupt).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
