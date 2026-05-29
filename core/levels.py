"""Support / resistance detection and persistence.

Method (Phase 2 default): **rolling extrema**. The resistance level is the
highest high, and the support level the lowest low, over the `lookback` bars
*immediately preceding* the latest closed bar. The latest closed bar is then
judged against those levels (a breakout = close above resistance; a support
break = close below support), so the level never includes the bar being tested
— that would be circular.

Levels are recomputed each scan and PERSISTED to ``sr_levels`` (SUMMARY §5).
The Position Manager (Phase 4) reads the persisted levels and never recomputes
ad hoc, so an open position is always judged against the same level the signal
scan saw.

All inputs are CLOSED bars, ordered oldest -> newest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

ROLLING_EXTREMA = "rolling_extrema"


@dataclass(frozen=True)
class SRLevels:
    support: Optional[float]
    resistance: Optional[float]
    method: str
    lookback: int


def detect_levels(
    highs: Sequence[float],
    lows: Sequence[float],
    *,
    lookback: int,
    method: str = ROLLING_EXTREMA,
) -> SRLevels:
    """Compute S/R from the `lookback` bars BEFORE the latest closed bar.

    `highs`/`lows` are the full series of closed bars (oldest -> newest); the
    last element is the latest closed bar and is EXCLUDED from the level window.
    Returns support/resistance = None when there is not enough history.
    """
    if method != ROLLING_EXTREMA:
        raise ValueError(f"unsupported S/R method: {method!r}")
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if len(highs) != len(lows):
        raise ValueError("highs and lows must be the same length")
    if len(highs) < lookback + 1:
        return SRLevels(None, None, method, lookback)

    window_highs = highs[-(lookback + 1):-1]
    window_lows = lows[-(lookback + 1):-1]
    return SRLevels(
        support=min(window_lows),
        resistance=max(window_highs),
        method=method,
        lookback=lookback,
    )


def persist_levels(
    conn: Connection,
    symbol: str,
    levels: SRLevels,
    bar_ts_utc: datetime,
) -> None:
    """Append a row to ``sr_levels`` (history kept; readers take the latest)."""
    conn.execute(
        text(
            "INSERT INTO dbo.sr_levels "
            "(symbol, support_price, resistance_price, method, lookback, bar_ts_utc) "
            "VALUES (:symbol, :support, :resistance, :method, :lookback, :bar_ts)"
        ),
        {
            "symbol": symbol,
            "support": levels.support,
            "resistance": levels.resistance,
            "method": levels.method,
            "lookback": levels.lookback,
            "bar_ts": bar_ts_utc,
        },
    )


def latest_levels(conn: Connection, symbol: str) -> Optional[SRLevels]:
    """Read the most recently computed S/R levels for a symbol (Phase 4 reader)."""
    row = conn.execute(
        text(
            "SELECT TOP 1 support_price, resistance_price, method, lookback "
            "FROM dbo.sr_levels WHERE symbol = :symbol "
            "ORDER BY computed_at_utc DESC"
        ),
        {"symbol": symbol},
    ).fetchone()
    if row is None:
        return None
    return SRLevels(
        support=float(row[0]) if row[0] is not None else None,
        resistance=float(row[1]) if row[1] is not None else None,
        method=row[2],
        lookback=int(row[3]),
    )
