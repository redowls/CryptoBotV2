r"""Phase 2 manual signal scan (NO orders placed).

For each active watchlist symbol:
  1. fetch the latest CLOSED 1h bars from Alpaca and cache them in market_bars
  2. detect support/resistance and persist to sr_levels
  3. run the TA engine -> (signal, confidence, breakout, S/R, hints)
  4. persist the result to the signals table
  5. print a one-line summary

This is the Phase 2 green-light test: every active symbol gets a signal +
0-100 confidence, breakouts visibly raise confidence, levels are persisted,
signals are persisted, and nothing trades.

Usage:
    .\.venv\Scripts\python.exe -m scripts.run_signal_scan            # all active symbols
    .\.venv\Scripts\python.exe -m scripts.run_signal_scan BTC/USD    # specific symbol(s)
"""
from __future__ import annotations

import json
import sys
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import text

from core import config
from core.db import connect_with_retry, get_active_credentials
from core.levels import detect_levels, persist_levels
from core.market_data import fetch_closed_bars, make_client, upsert_bars
from core.ta_engine import TAParams, evaluate

PROVIDER = "alpaca"


def _active_symbols(explicit: list[str]) -> list[str]:
    if explicit:
        return explicit
    with connect_with_retry() as conn:
        rows = conn.execute(
            text("SELECT symbol FROM dbo.watchlist WHERE is_active = 1 ORDER BY id")
        ).fetchall()
    return [r[0] for r in rows]


def _persist_signal(conn, sig, snapshot_json: str) -> None:
    """Upsert into signals on (symbol, bar_ts_utc) so a re-scan refreshes."""
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


def _fmt(x: Optional[float], nd: int = 2) -> str:
    return f"{x:,.{nd}f}" if isinstance(x, (int, float)) else "   -"


def main(argv: list[str]) -> int:
    load_dotenv()

    environment = config.get_str("environment", "paper")
    timeframe = config.get_str("scan.timeframe", "1Hour")
    limit = config.get_int("scan.bars_to_fetch", 250)
    params = TAParams.from_config()

    api_key, api_secret = get_active_credentials(PROVIDER, environment)
    client = make_client(api_key, api_secret)
    api_key = api_secret = ""  # drop plaintext once the client holds it

    symbols = _active_symbols(argv)
    if not symbols:
        print("No active watchlist symbols. Add one and re-run.", file=sys.stderr)
        return 1

    print(f"\nSignal scan - env={environment} timeframe={timeframe} "
          f"min_bars={params.min_bars()}\n")
    header = (f"{'SYMBOL':<10} {'SETUP':<6} {'CONF':>6} {'BRK':>4} "
              f"{'RSI':>6} {'SUPPORT':>14} {'RESIST':>14} {'ENTRY':>14} {'STOP':>14}")
    print(header)
    print("-" * len(header))

    failures = 0
    any_signal = False
    for symbol in symbols:
        try:
            bars = fetch_closed_bars(
                client, symbol, timeframe=timeframe, limit=limit
            )
            sig = evaluate(symbol, bars, params)

            levels = detect_levels(
                [b.high for b in bars], [b.low for b in bars],
                lookback=params.sr_lookback, method=params.sr_method,
            ) if bars else None

            snapshot_json = json.dumps(sig.snapshot, default=str)
            with connect_with_retry() as conn:
                with conn.begin():
                    upsert_bars(conn, bars, timeframe=timeframe)
                    if levels is not None and bars:
                        persist_levels(conn, symbol, levels, bars[-1].ts)
                    _persist_signal(conn, sig, snapshot_json)

            rsi = sig.snapshot.get("rsi") if isinstance(sig.snapshot, dict) else None
            setup = "BUY" if sig.signal else "----"
            brk = "yes" if sig.breakout_confirmed else "no"
            any_signal = any_signal or sig.signal
            print(f"{symbol:<10} {setup:<6} {sig.confidence:>6.1f} {brk:>4} "
                  f"{_fmt(rsi, 1):>6} {_fmt(sig.support_level):>14} "
                  f"{_fmt(sig.resistance_level):>14} {_fmt(sig.entry_hint):>14} "
                  f"{_fmt(sig.stop_hint):>14}  ({len(bars)} bars)")
        except Exception as exc:
            failures += 1
            print(f"{symbol:<10} ERROR  {type(exc).__name__}: {exc}")

    print()
    if failures:
        print(f"Scan completed with {failures} error(s) - see above.")
        return 1
    print("Scan complete. No orders placed (Phase 2). "
          f"{'A BUY setup is present above.' if any_signal else 'No BUY setups this bar.'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
