"""Orchestrator — the hourly trade cycle (SUMMARY §3 data-flow, §12 cadence).

Each cycle, for every active watchlist symbol:

    market data -> levels -> TA engine (with breakout boost)
      -> persist bars / S/R / signal
      -> ENTRY pass: if NOT already held AND signal AND confidence >= floor
         AND not in cool-down -> size -> place_entry(ta_signal=True) -> record

The TA gate sits before sizing/execution by construction (invariant #1): we
only ever reach :func:`core.execution.place_entry` after ``sig.signal`` is True,
and that function independently refuses a non-True gate.

Entry-side rules enforced here:
  - One entry per symbol per bar; skip symbols already held — no stacking (§12).
  - Cool-down after a stop-out: no re-entry within ``cooldown.bars_after_stop``
    bars of the symbol's last price_sl/support_break exit.
  - Portfolio caps (max open positions, aggregate open risk) and spendable cash
    are tracked across the cycle so later symbols respect capital already
    committed earlier in the SAME cycle.

EXIT management (dynamic TP/SL, trailing, support-break vs price-SL) is the
Phase 4 Position Manager and is intentionally NOT implemented here — Phase 3
delivers entries + initial TP/SL only. The exit pass below is a clearly-marked
placeholder so phase discipline is visible in the code.

All money/price values are plain float; the DB stores DECIMAL(38,18).
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from core import config
from core.db import connect_with_retry, get_active_credentials
from core.execution import make_trading_client, place_entry, record_position
from core.levels import detect_levels, persist_levels
from core.market_data import (
    fetch_closed_bars,
    make_client,
    timeframe_duration,
    upsert_bars,
)
from core.sizing import compute_size, load_active_risk_profile
from core.ta_engine import TAParams, TASignal, evaluate

PROVIDER = "alpaca"


# --------------------------------------------------------------------------
# DB readers
# --------------------------------------------------------------------------
def _active_symbols(explicit: list[str]) -> list[str]:
    if explicit:
        return explicit
    with connect_with_retry() as conn:
        rows = conn.execute(
            text("SELECT symbol FROM dbo.watchlist WHERE is_active = 1 ORDER BY id")
        ).fetchall()
    return [r[0] for r in rows]


def _open_positions_state(conn: Connection) -> tuple[set[str], int, float]:
    """Return (held symbols, open count, aggregate open risk $) from positions.

    Aggregate open risk = sum of qty * (entry - sl) over open positions with a
    price-based SL below entry — the same risk unit the sizer budgets against.
    """
    rows = conn.execute(
        text(
            "SELECT symbol, qty, entry_price, sl_price "
            "FROM dbo.positions WHERE status = 'open'"
        )
    ).fetchall()
    held: set[str] = set()
    open_risk = 0.0
    for symbol, qty, entry, sl in rows:
        held.add(symbol)
        if sl is not None:
            q, e, s = float(qty), float(entry), float(sl)
            if s < e:
                open_risk += q * (e - s)
    return held, len(rows), open_risk


def _in_cooldown(
    conn: Connection, symbol: str, current_bar_ts: datetime, cooldown_bars: int, bar_seconds: float
) -> bool:
    """True if the symbol was stopped out within the cool-down window (§12)."""
    if cooldown_bars <= 0:
        return False
    row = conn.execute(
        text(
            "SELECT TOP 1 closed_at_utc FROM dbo.trade_history "
            "WHERE symbol = :symbol "
            "  AND exit_reason IN ('price_sl', 'support_break') "
            "ORDER BY closed_at_utc DESC"
        ),
        {"symbol": symbol},
    ).fetchone()
    if row is None or row[0] is None:
        return False
    last_stop = row[0]
    elapsed = (current_bar_ts - last_stop).total_seconds()
    return elapsed < cooldown_bars * bar_seconds


def _persist_signal(conn: Connection, sig: TASignal, snapshot_json: str) -> None:
    """Upsert the signal on (symbol, bar_ts_utc) — same shape as the Phase 2 scan."""
    conn.execute(
        text(
            "MERGE dbo.signals AS t "
            "USING (SELECT :symbol AS symbol, :bar_ts AS bar_ts_utc) AS s "
            "ON (t.symbol = s.symbol AND t.bar_ts_utc = s.bar_ts_utc) "
            "WHEN MATCHED THEN UPDATE SET "
            "  signal = :signal, confidence = :confidence, "
            "  breakout_confirmed = :breakout, entry_hint = :entry, stop_hint = :stop, "
            "  support_level = :support, resistance_level = :resistance, "
            "  indicator_snapshot = :snapshot, created_at_utc = SYSUTCDATETIME() "
            "WHEN NOT MATCHED THEN INSERT "
            "  (symbol, bar_ts_utc, signal, confidence, breakout_confirmed, "
            "   entry_hint, stop_hint, support_level, resistance_level, indicator_snapshot) "
            "  VALUES (:symbol, :bar_ts, :signal, :confidence, :breakout, "
            "          :entry, :stop, :support, :resistance, :snapshot);"
        ),
        {
            "symbol": sig.symbol,
            "bar_ts": sig.bar_ts,
            "signal": 1 if sig.signal else 0,
            "confidence": round(sig.confidence, 2),
            "breakout": 1 if sig.breakout_confirmed else 0,
            "entry": sig.entry_hint,
            "stop": sig.stop_hint,
            "support": sig.support_level,
            "resistance": sig.resistance_level,
            "snapshot": snapshot_json,
        },
    )


def _force_signal(sig: TASignal, floor: float) -> TASignal:
    """DEV ONLY: force a BUY setup so an entry can be exercised on paper.

    Keeps the real entry/stop/support hints (so sizing + SL are realistic) but
    flips the gate True and lifts confidence to the trade floor if needed. This
    fakes the TA *output*, not the gate itself — place_entry still requires a
    True signal, which is exactly what we are synthesizing.
    """
    bumped = sig.confidence if sig.confidence >= floor else floor
    return replace(sig, signal=True, confidence=bumped)


# --------------------------------------------------------------------------
# Cycle
# --------------------------------------------------------------------------
def run_cycle(
    *,
    symbols: Optional[list[str]] = None,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """Run one hourly trade cycle. Returns 0 on success, 1 if any symbol errored.

    dry_run: size and report intended entries but place NO orders.
    force:   DEV ONLY — synthesize a BUY signal for the given symbols so an
             entry can be exercised end-to-end (the Phase 3 green-light test).
    """
    environment = config.get_str("environment", "paper")
    timeframe = config.get_str("scan.timeframe", "1Hour")
    limit = config.get_int("scan.bars_to_fetch", 250)
    tp_r = config.get_float("tp.r_multiple", 2.0)
    tif = config.get_str("execution.time_in_force", "gtc")
    poll_attempts = config.get_int("execution.poll_attempts", 5)
    poll_delay = config.get_float("execution.poll_delay_secs", 1.0)
    min_notional = config.get_float("execution.min_notional", 1.0)
    cooldown_bars = config.get_int("cooldown.bars_after_stop", 3)
    params = TAParams.from_config()
    bar_seconds = timeframe_duration(timeframe).total_seconds()

    api_key, api_secret = get_active_credentials(PROVIDER, environment)
    data_client = make_client(api_key, api_secret)
    trading_client = make_trading_client(api_key, api_secret, paper=(environment != "live"))
    api_key = api_secret = ""  # drop plaintext once the clients hold it

    account = trading_client.get_account()
    equity = float(account.equity)
    cash = float(account.cash)

    with connect_with_retry() as conn:
        profile = load_active_risk_profile(conn)
        held, open_count, open_risk = _open_positions_state(conn)

    syms = symbols if symbols else _active_symbols([])
    if not syms:
        print("No active watchlist symbols. Add one and re-run.")
        return 1

    mode = "DRY-RUN (no orders)" if dry_run else f"LIVE {environment}"
    print(
        f"\nTrade cycle - {mode} | equity={equity:,.2f} cash={cash:,.2f} | "
        f"open={open_count}/{profile.max_open_positions} "
        f"open_risk={open_risk:,.2f}/{equity * profile.max_portfolio_risk_pct / 100:,.2f} | "
        f"floor={profile.min_confidence_to_trade:.0f} tp_r={tp_r}"
    )
    if force:
        print("  [DEV] --force active: BUY signals synthesized for listed symbols.")
    print()

    failures = 0
    for symbol in syms:
        try:
            bars = fetch_closed_bars(data_client, symbol, timeframe=timeframe, limit=limit)
            sig = evaluate(symbol, bars, params)
            if force:
                sig = _force_signal(sig, profile.min_confidence_to_trade)

            levels = (
                detect_levels(
                    [b.high for b in bars], [b.low for b in bars],
                    lookback=params.sr_lookback, method=params.sr_method,
                )
                if bars else None
            )

            snapshot_json = json.dumps(sig.snapshot, default=str)
            with connect_with_retry() as conn:
                with conn.begin():
                    upsert_bars(conn, bars, timeframe=timeframe)
                    if levels is not None and bars:
                        persist_levels(conn, symbol, levels, bars[-1].ts)
                    _persist_signal(conn, sig, snapshot_json)

            # --- ENTRY decision -------------------------------------------
            if symbol in held:
                print(f"{symbol:<10} HELD   - skip (no stacking)")
                continue
            if not sig.signal:
                print(f"{symbol:<10} ----   - no BUY setup (conf={sig.confidence:.0f})")
                continue

            with connect_with_retry() as conn:
                cooling = _in_cooldown(conn, symbol, sig.bar_ts, cooldown_bars, bar_seconds)
            if cooling:
                print(f"{symbol:<10} COOL   - in cool-down after a recent stop-out")
                continue

            size = compute_size(
                confidence=sig.confidence,
                equity=equity,
                cash_available=cash,
                entry_price=sig.entry_hint,
                stop_price=sig.stop_hint,
                profile=profile,
                open_position_count=open_count,
                current_open_risk=open_risk,
                min_notional=min_notional,
            )
            if not size.accepted:
                print(f"{symbol:<10} SKIP   - {size.reason}")
                continue

            tp_price = sig.entry_hint + tp_r * (sig.entry_hint - sig.stop_hint)

            if dry_run:
                print(
                    f"{symbol:<10} WOULD BUY qty={size.qty:.8f} @~{sig.entry_hint:,.2f} "
                    f"(notional {size.notional:,.2f}, risk {size.risk_pct_used:.2f}%) "
                    f"SL={sig.stop_hint:,.2f} TP={tp_price:,.2f} "
                    f"support={sig.support_level if sig.support_level else '-'} "
                    f"conf={sig.confidence:.0f} brk={'Y' if sig.breakout_confirmed else 'N'}"
                )
                continue

            # --- EXECUTE (gate enforced inside place_entry) ----------------
            fill = place_entry(
                trading_client,
                ta_signal=sig.signal,
                symbol=symbol,
                qty=size.qty,
                time_in_force=tif,
                poll_attempts=poll_attempts,
                poll_delay=poll_delay,
            )
            if not fill.is_filled:
                print(f"{symbol:<10} NOFILL - order {fill.order_id} status={fill.status}")
                continue

            with connect_with_retry() as conn:
                with conn.begin():
                    pos_id = record_position(
                        conn,
                        symbol=symbol,
                        fill=fill,
                        sl_price=sig.stop_hint,
                        support_break_price=sig.support_level,
                        tp_price=tp_price,
                        confidence=sig.confidence,
                        breakout_confirmed=sig.breakout_confirmed,
                        risk_pct_used=size.risk_pct_used,
                    )

            # Update in-cycle accounting so later symbols respect committed capital.
            held.add(symbol)
            open_count += 1
            cash -= fill.filled_qty * fill.avg_price
            if sig.stop_hint < fill.avg_price:
                open_risk += fill.filled_qty * (fill.avg_price - sig.stop_hint)

            partial = "" if fill.filled_qty >= size.qty else " (PARTIAL)"
            print(
                f"{symbol:<10} BOUGHT pos#{pos_id} qty={fill.filled_qty:.8f}{partial} "
                f"@{fill.avg_price:,.2f} SL={sig.stop_hint:,.2f} TP={tp_price:,.2f} "
                f"support={sig.support_level if sig.support_level else '-'} "
                f"conf={sig.confidence:.0f} risk={size.risk_pct_used:.2f}% "
                f"order={fill.order_id}"
            )
        except Exception as exc:
            failures += 1
            print(f"{symbol:<10} ERROR  {type(exc).__name__}: {exc}")

    # --- EXIT pass: Phase 4 Position Manager (not built yet) --------------
    print(
        "\nExit checks: deferred to Phase 4 (core/position_manager.py). "
        f"{open_count} open position(s) carry static TP/SL + support-break triggers."
    )

    print()
    if failures:
        print(f"Cycle completed with {failures} error(s) - see above.")
        return 1
    print("Cycle complete.")
    return 0
