r"""Phase 7 green-light — offline verification of the Observability + Alerting logic.

Exercises the PURE helpers across the phase (no DB, no broker, no network, no
Telegram): the log-record shape, the heartbeat staleness math, the alert
severity gate + dedup window + message formatting, and the daemon's scheduling /
fault-classification helpers.

The intent is the same structural guarantee as the other phases' gates: the
decision logic is pinned deterministically so the LIVE wiring only has to prove
the I/O (a real heartbeat row + a real Telegram message).

Run:
    .\.venv\Scripts\python.exe -m scripts.verify_phase7
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import alerting, daemon, heartbeat, obs

_passed = 0
_failed = 0


def check(name: str, got, want) -> None:
    global _passed, _failed
    if got == want:
        _passed += 1
        print(f"PASS  {name}")
    else:
        _failed += 1
        print(f"FAIL  {name}: got {got!r}, want {want!r}")


def check_true(name: str, cond: bool) -> None:
    check(name, bool(cond), True)


def main() -> int:
    now = datetime(2026, 5, 31, 14, 30, 0, tzinfo=timezone.utc)

    # --- obs: pure record shape -------------------------------------------
    check("iso_utc ends in Z", obs.iso_utc(now).endswith("Z"), True)
    rec = obs.event_dict("cycle_start", "info", symbols=12)
    check("event_dict level uppercased", rec["level"], "INFO")
    check("event_dict carries event", rec["event"], "cycle_start")
    check("event_dict carries fields", rec["symbols"], 12)
    check("event_dict leads with ts", list(rec.keys())[0], "ts")

    # --- heartbeat: staleness math ----------------------------------------
    fresh = now - timedelta(seconds=60)
    old = now - timedelta(seconds=1000)
    check("seconds_since computes age", round(heartbeat.seconds_since(fresh, now)), 60)
    check("is_stale: fresh within threshold", heartbeat.is_stale(fresh, 900, now), False)
    check("is_stale: old beyond threshold", heartbeat.is_stale(old, 900, now), True)
    check("is_stale: missing heartbeat is stale", heartbeat.is_stale(None, 900, now), True)
    # naive-UTC timestamps (the DB hands these back) must work too
    check("is_stale: naive-UTC fresh", heartbeat.is_stale(fresh.replace(tzinfo=None), 900, now), False)

    # --- alerting: severity gate ------------------------------------------
    check("rank critical > warning", alerting.severity_rank("critical") > alerting.severity_rank("warning"), True)
    check("rank unknown == warning", alerting.severity_rank("bogus"), alerting.severity_rank("warning"))
    check("should_send: critical >= warning floor", alerting.should_send("critical", "warning"), True)
    check("should_send: info < warning floor", alerting.should_send("info", "warning"), False)
    check("should_send: equal passes", alerting.should_send("warning", "warning"), True)

    # --- alerting: dedup window -------------------------------------------
    check("dedup_key stable", alerting.dedup_key("critical", " DB unreachable "), "critical::DB unreachable")
    recent = (now - timedelta(seconds=100)).isoformat().replace("+00:00", "Z")
    stale_sent = (now - timedelta(seconds=5000)).isoformat().replace("+00:00", "Z")
    check("is_duplicate: within window", alerting.is_duplicate(recent, 3600, now), True)
    check("is_duplicate: past window", alerting.is_duplicate(stale_sent, 3600, now), False)
    check("is_duplicate: never sent", alerting.is_duplicate(None, 3600, now), False)
    check("is_duplicate: garbage ts is not a dup", alerting.is_duplicate("not-a-date", 3600, now), False)

    # --- alerting: message formatting -------------------------------------
    msg = alerting.format_telegram("Heartbeat stalled", "no beat in 15m", "critical", host="vps-1")
    check_true("format: severity in header", "[CRITICAL]" in msg)
    check_true("format: subject present", "Heartbeat stalled" in msg)
    check_true("format: body present", "no beat in 15m" in msg)
    check_true("format: host present", "host: vps-1" in msg)

    # --- daemon: scheduling math ------------------------------------------
    # 14:30 with cycle_minute=1 -> next fire 15:01
    nxt = daemon.next_cycle_time(now, 1)
    check("next_cycle_time rolls to next hour", (nxt.hour, nxt.minute), (15, 1))
    # exactly on the minute still advances (strictly after now)
    on_min = datetime(2026, 5, 31, 14, 1, 0, tzinfo=timezone.utc)
    nxt2 = daemon.next_cycle_time(on_min, 1)
    check("next_cycle_time strictly after now", (nxt2.hour, nxt2.minute), (15, 1))
    # before the minute, fires this hour
    before = datetime(2026, 5, 31, 14, 0, 30, tzinfo=timezone.utc)
    check("next_cycle_time fires this hour when before minute", daemon.next_cycle_time(before, 1).hour, 14)

    # --- daemon: fault classification -------------------------------------
    check_true("auth error detected (401)", daemon.looks_like_auth_error(RuntimeError("HTTP 401 unauthorized")))
    check_true("auth error detected (forbidden)", daemon.looks_like_auth_error(ValueError("forbidden: invalid api key")))
    check("non-auth error not misclassified", daemon.looks_like_auth_error(ValueError("rate limited")), False)
    check_true("db error detected (operational)", daemon.looks_like_db_error(RuntimeError("OperationalError: login timeout expired")))
    check("non-db error not misclassified", daemon.looks_like_db_error(ValueError("bad symbol")), False)

    print(f"\n{_passed} passed, {_failed} failed.")
    if _failed == 0:
        print(
            "PHASE 7 LOGIC GREEN LIGHT (offline). Live wiring still required: "
            "apply db/07_schema_phase7.sql, store Telegram creds (provider='telegram'), "
            "run the daemon + monitor, and confirm a real heartbeat row + a real "
            "Telegram alert on a forced stall."
        )
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
