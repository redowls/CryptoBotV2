r"""Phase 5 admin tool — review watchlist filter results (read-only).

For each symbol (default: ALL watchlist rows, active AND inactive — so you can
vet a candidate BEFORE activating it), fetch the latest closed bars + a live
quote, run the liquidity / spread / volatility filters, and print each filter's
value, threshold, and the overall verdict. Places NO orders and writes NOTHING.

This is the gate you run before flipping a new symbol's `is_active` to 1: it
shows whether the symbol would actually clear the entry filters on live data.

Usage:
    .\.venv\Scripts\python.exe -m scripts.run_watchlist_filters            # all watchlist symbols
    .\.venv\Scripts\python.exe -m scripts.run_watchlist_filters BTC/USD    # specific symbol(s)
    .\.venv\Scripts\python.exe -m scripts.run_watchlist_filters --active   # only is_active = 1
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from core import config, db
from core.market_data import fetch_closed_bars, make_client
from core.watchlist_filters import FilterParams, screen_symbol


def _watchlist(active_only: bool) -> list[tuple[str, bool]]:
    sql = "SELECT symbol, is_active FROM dbo.watchlist"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY id"
    with db.connect_with_retry() as conn:
        rows = conn.execute(db.text(sql)).fetchall()
    return [(r[0], bool(r[1])) for r in rows]


def main(argv: list[str]) -> int:
    load_dotenv()
    active_only = "--active" in argv
    explicit = [a for a in argv if not a.startswith("--")]

    params = FilterParams.from_config()
    if explicit:
        symbols: list[tuple[str, bool | None]] = [(s, None) for s in explicit]
    else:
        symbols = _watchlist(active_only)
    if not symbols:
        print("No watchlist symbols match.")
        return 1

    environment = config.get_str("environment", "paper")
    key, secret = db.get_active_credentials("alpaca", environment)
    data_client = make_client(key, secret)
    tf = config.get_str("scan.timeframe", "1Hour")
    limit = config.get_int("scan.bars_to_fetch", 250)

    print(
        f"\nWatchlist filters (read-only) | "
        f"liq>={params.min_24h_quote_volume:,.0f}  "
        f"spread<={params.max_spread_pct:.3f}%  "
        f"atr {params.min_atr_pct:.2f}-{params.max_atr_pct:.2f}%\n"
    )

    eligible = 0
    for symbol, is_active in symbols:
        try:
            bars = fetch_closed_bars(data_client, symbol, timeframe=tf, limit=limit)
        except Exception as exc:
            print(f"{symbol:<12} ERROR fetching bars: {type(exc).__name__}: {exc}")
            continue
        if not bars:
            print(f"{symbol:<12} no closed bars")
            continue

        res = screen_symbol(data_client, symbol, bars, params)
        flag = "" if is_active is None else (" [active]" if is_active else " [inactive]")
        verdict = "ELIGIBLE" if res.passed else "SKIP"
        if res.passed:
            eligible += 1
        print(f"{symbol:<12}{flag} {verdict}")
        for c in res.checks:
            mark = "ok" if c.passed else "XX"
            print(f"    [{mark}] {c.name:<11} {c.detail}  (need {c.threshold})")

    print(f"\n{eligible}/{len(symbols)} eligible for entry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
