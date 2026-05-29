"""Pure technical-indicator functions.

No I/O, no Alpaca, no DB — just math on sequences of floats. Every function
takes already-CLOSED bar data (the look-ahead-bias rule lives upstream in
``market_data``/``ta_engine``; these functions just compute on whatever they
are given) and returns the LATEST value of the indicator, or ``None`` when
there is not enough data to compute it.

Conventions:
- Inputs are ordered oldest -> newest.
- EMA/ATR use the standard recursive smoothing; RSI/ATR use Wilder's smoothing
  (the de-facto standard, matching most charting platforms).
- Returning the latest scalar keeps callers simple; the full series is rarely
  needed and easy to rebuild if it ever is.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


def ema(values: Sequence[float], period: int) -> Optional[float]:
    """Latest Exponential Moving Average, or None if fewer than `period` points.

    Seeded with the simple average of the first `period` values, then smoothed
    with alpha = 2 / (period + 1).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    ema_val = seed
    for v in values[period:]:
        ema_val = (v - ema_val) * alpha + ema_val
    return ema_val


def rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    """Latest Wilder RSI (0-100), or None if fewer than `period + 1` points.

    Needs `period + 1` closes to form `period` deltas. Returns 100.0 when there
    is no downside over the window (avg loss == 0).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period + 1:
        return None

    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder smoothing across the remaining deltas.
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> Optional[float]:
    """Latest Wilder Average True Range, or None if fewer than `period + 1` bars.

    True range uses the prior close, so `period + 1` bars are required to form
    `period` true ranges.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs, lows, closes must be the same length")
    if n < period + 1:
        return None

    true_ranges = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    atr_val = sum(true_ranges[:period]) / period
    for i in range(period, len(true_ranges)):
        atr_val = (atr_val * (period - 1) + true_ranges[i]) / period
    return atr_val


def volume_zscore(volumes: Sequence[float], lookback: int = 20) -> Optional[float]:
    """Z-score of the latest volume vs the prior `lookback` bars.

    Measures how unusual the most recent bar's volume is relative to its recent
    baseline (the bars BEFORE it), used for volume confirmation. Returns None if
    there are fewer than `lookback + 1` bars, or 0.0 if the baseline has no
    variance.
    """
    if lookback <= 1:
        raise ValueError("lookback must be > 1")
    if len(volumes) < lookback + 1:
        return None

    window = volumes[-(lookback + 1):-1]  # the `lookback` bars before the latest
    latest = volumes[-1]
    mean = sum(window) / lookback
    var = sum((v - mean) ** 2 for v in window) / lookback
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (latest - mean) / std
