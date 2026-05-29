"""Market data: fetch OHLCV from Alpaca, keep only CLOSED bars, cache them.

Hourly cadence means ~24x the API calls of daily, so bars are cached in
``market_bars`` and re-read rather than re-fetched (SUMMARY §12). The
look-ahead-bias invariant is enforced HERE: :func:`fetch_closed_bars` drops the
still-forming bar, so nothing downstream can accidentally evaluate an
incomplete hour.

Timestamps are the bar's START time, stored as naive UTC (the DB column is
DATETIME2 with no offset; everything in this project is UTC by convention).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from sqlalchemy import text
from sqlalchemy.engine import Connection

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# Alpaca data endpoint hiccups (maintenance windows, network blips) surface as
# connection/timeout errors. Tolerate them with bounded backoff rather than
# failing the whole scan on a single transient (SUMMARY §10).
_RETRYABLE_HTTP = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)

_UNIT = {
    "Min": (TimeFrameUnit.Minute, lambda n: timedelta(minutes=n)),
    "Minute": (TimeFrameUnit.Minute, lambda n: timedelta(minutes=n)),
    "Hour": (TimeFrameUnit.Hour, lambda n: timedelta(hours=n)),
    "Day": (TimeFrameUnit.Day, lambda n: timedelta(days=n)),
}
_TF_RE = re.compile(r"^(\d+)(Min|Minute|Hour|Day)$")


@dataclass(frozen=True)
class Bar:
    """One CLOSED OHLCV bar. ts is the bar's START time (naive UTC)."""
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: Optional[int]
    vwap: Optional[float]


def _parse_timeframe(tf: str) -> tuple[TimeFrame, timedelta]:
    m = _TF_RE.match(tf)
    if not m:
        raise ValueError(f"unrecognized timeframe {tf!r} (e.g. '1Hour', '15Min')")
    amount, unit_name = int(m.group(1)), m.group(2)
    unit, duration = _UNIT[unit_name]
    return TimeFrame(amount, unit), duration(amount)


def make_client(api_key: str, api_secret: str) -> CryptoHistoricalDataClient:
    return CryptoHistoricalDataClient(api_key, api_secret)


def _get_bars_with_retry(
    client: CryptoHistoricalDataClient,
    request: CryptoBarsRequest,
    max_attempts: int = 3,
    base_delay: float = 2.0,
):
    """Call Alpaca for bars, retrying transient connection/timeout errors."""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return client.get_crypto_bars(request)
        except _RETRYABLE_HTTP as exc:
            last_exc = exc
            if attempt < max_attempts:
                time.sleep(base_delay * attempt)
    assert last_exc is not None
    raise last_exc


def fetch_closed_bars(
    client: CryptoHistoricalDataClient,
    symbol: str,
    *,
    timeframe: str = "1Hour",
    limit: int = 250,
    now: Optional[datetime] = None,
) -> list[Bar]:
    """Fetch the latest `limit` CLOSED bars for `symbol`, oldest -> newest.

    The still-forming bar is dropped: a bar starting at `ts` covers
    ``[ts, ts + duration)`` and is only CLOSED once ``now >= ts + duration``.
    """
    tf, duration = _parse_timeframe(timeframe)
    now = now or datetime.now(timezone.utc)
    # Pull a little extra history to be safe, then trim to `limit` closed bars.
    start = now - duration * (limit + 5)

    request = CryptoBarsRequest(
        symbol_or_symbols=symbol, timeframe=tf, start=start
    )
    barset = _get_bars_with_retry(client, request)
    raw = barset.data.get(symbol, [])

    bars: list[Bar] = []
    for b in raw:
        ts = b.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        # Closed-bar gate: skip the forming bar (and anything in the future).
        if ts + duration > now:
            continue
        bars.append(
            Bar(
                symbol=symbol,
                ts=ts.astimezone(timezone.utc).replace(tzinfo=None),
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
                trade_count=int(b.trade_count) if b.trade_count is not None else None,
                vwap=float(b.vwap) if b.vwap is not None else None,
            )
        )

    return bars[-limit:]


def upsert_bars(
    conn: Connection, bars: list[Bar], *, timeframe: str = "1Hour"
) -> int:
    """MERGE bars into ``market_bars`` (insert new, refresh existing). Returns count."""
    if not bars:
        return 0
    stmt = text(
        "MERGE dbo.market_bars AS t "
        "USING (SELECT :symbol AS symbol, :timeframe AS timeframe, :ts AS ts_utc) AS s "
        "ON (t.symbol = s.symbol AND t.timeframe = s.timeframe AND t.ts_utc = s.ts_utc) "
        "WHEN MATCHED THEN UPDATE SET "
        "  open_price = :open, high_price = :high, low_price = :low, "
        "  close_price = :close, volume = :volume, trade_count = :trade_count, vwap = :vwap "
        "WHEN NOT MATCHED THEN INSERT "
        "  (symbol, timeframe, ts_utc, open_price, high_price, low_price, "
        "   close_price, volume, trade_count, vwap) "
        "  VALUES (:symbol, :timeframe, :ts, :open, :high, :low, "
        "          :close, :volume, :trade_count, :vwap);"
    )
    for b in bars:
        conn.execute(
            stmt,
            {
                "symbol": b.symbol,
                "timeframe": timeframe,
                "ts": b.ts,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "trade_count": b.trade_count,
                "vwap": b.vwap,
            },
        )
    return len(bars)
