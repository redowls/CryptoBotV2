"""TA Signal Engine — the gatekeeper (SUMMARY §3, CLAUDE.md invariant #1).

Consumes CLOSED bars + tuned parameters and emits a BUY *setup* signal plus a
0-100 confidence score. Nothing downstream may buy unless ``signal`` is True;
confidence only ever scales SIZE (Phase 3), never the safety rails.

Confidence is a weighted sum of independent components:

  - trend     EMA(fast) > EMA(slow)                          -> weights.trend
  - momentum  rsi_bull_min <= RSI < rsi_overbought           -> weights.momentum
  - volume    volume z-score >= vol_z_min                    -> weights.volume
  - breakout  confirmed close ABOVE resistance               -> weights.breakout
              (volume-confirmed if breakout.require_volume)

The BUY setup gate is ``trend AND momentum`` — the structural core of the
signal. Volume and the resistance breakout are confidence boosters on top: a
breakout visibly RAISES the score (decision #10) but cannot, by itself, create
a signal without trend+momentum alignment.

Everything operates on the latest CLOSED bar only — no look-ahead bias. The
resistance level is computed from the bars BEFORE the latest closed bar, so a
close above it is a genuine break (a wick is not a break — only the close
counts).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core import config, indicators
from core.levels import SRLevels, detect_levels
from core.market_data import Bar


@dataclass(frozen=True)
class TAParams:
    """Tunable strategy parameters (mirrors the Phase 2 app_config keys)."""
    ema_fast_period: int
    ema_slow_period: int
    rsi_period: int
    rsi_bull_min: float
    rsi_overbought: float
    atr_period: int
    atr_stop_mult: float
    vol_lookback: int
    vol_z_min: float
    sr_method: str
    sr_lookback: int
    breakout_require_volume: bool
    breakout_vol_z_min: float
    w_trend: float
    w_momentum: float
    w_volume: float
    w_breakout: float

    @classmethod
    def from_config(cls) -> "TAParams":
        return cls(
            ema_fast_period=config.get_int("ta.ema_fast_period", 12),
            ema_slow_period=config.get_int("ta.ema_slow_period", 26),
            rsi_period=config.get_int("ta.rsi_period", 14),
            rsi_bull_min=config.get_float("ta.rsi_bull_min", 50.0),
            rsi_overbought=config.get_float("ta.rsi_overbought", 70.0),
            atr_period=config.get_int("ta.atr_period", 14),
            atr_stop_mult=config.get_float("ta.atr_stop_mult", 1.5),
            vol_lookback=config.get_int("ta.vol_lookback", 20),
            vol_z_min=config.get_float("ta.vol_z_min", 0.5),
            sr_method=config.get_str("sr.method", "rolling_extrema"),
            sr_lookback=config.get_int("sr.lookback", 20),
            breakout_require_volume=config.get_bool("breakout.require_volume", True),
            breakout_vol_z_min=config.get_float("breakout.vol_z_min", 1.0),
            w_trend=config.get_float("weights.trend", 40.0),
            w_momentum=config.get_float("weights.momentum", 25.0),
            w_volume=config.get_float("weights.volume", 15.0),
            w_breakout=config.get_float("weights.breakout", 20.0),
        )

    def min_bars(self) -> int:
        """Bars required before any signal can be computed."""
        return max(
            self.ema_slow_period,
            self.rsi_period + 1,
            self.atr_period + 1,
            self.vol_lookback + 1,
            self.sr_lookback + 1,
        )


@dataclass(frozen=True)
class TASignal:
    symbol: str
    bar_ts: object                 # datetime of the latest closed bar
    signal: bool                   # BUY setup present (the gate)
    confidence: float              # 0-100
    breakout_confirmed: bool
    entry_hint: Optional[float]
    stop_hint: Optional[float]     # price-based SL candidate
    support_level: Optional[float]
    resistance_level: Optional[float]
    snapshot: dict                 # raw indicator values + component breakdown


def _insufficient(symbol: str, bars: list[Bar], reason: str) -> TASignal:
    return TASignal(
        symbol=symbol,
        bar_ts=bars[-1].ts if bars else None,
        signal=False,
        confidence=0.0,
        breakout_confirmed=False,
        entry_hint=bars[-1].close if bars else None,
        stop_hint=None,
        support_level=None,
        resistance_level=None,
        snapshot={"reason": reason, "bars": len(bars)},
    )


def evaluate(symbol: str, bars: list[Bar], params: TAParams) -> TASignal:
    """Evaluate the latest CLOSED bar and return the signal + confidence."""
    if len(bars) < params.min_bars():
        return _insufficient(
            symbol, bars, f"need >= {params.min_bars()} closed bars"
        )

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    volumes = [b.volume for b in bars]
    latest = bars[-1]

    ema_fast = indicators.ema(closes, params.ema_fast_period)
    ema_slow = indicators.ema(closes, params.ema_slow_period)
    rsi = indicators.rsi(closes, params.rsi_period)
    atr = indicators.atr(highs, lows, closes, params.atr_period)
    vol_z = indicators.volume_zscore(volumes, params.vol_lookback)
    levels: SRLevels = detect_levels(
        highs, lows, lookback=params.sr_lookback, method=params.sr_method
    )

    if None in (ema_fast, ema_slow, rsi, atr, vol_z):
        return _insufficient(symbol, bars, "indicator warmup incomplete")

    # --- Components -------------------------------------------------------
    trend_aligned = ema_fast > ema_slow
    momentum_ok = params.rsi_bull_min <= rsi < params.rsi_overbought
    volume_ok = vol_z >= params.vol_z_min

    breakout_confirmed = False
    if levels.resistance is not None and latest.close > levels.resistance:
        if not params.breakout_require_volume or vol_z >= params.breakout_vol_z_min:
            breakout_confirmed = True

    # --- Confidence -------------------------------------------------------
    confidence = 0.0
    if trend_aligned:
        confidence += params.w_trend
    if momentum_ok:
        confidence += params.w_momentum
    if volume_ok:
        confidence += params.w_volume
    if breakout_confirmed:
        confidence += params.w_breakout
    confidence = max(0.0, min(100.0, confidence))

    # The structural BUY gate: trend AND momentum must align. Volume/breakout
    # only add confidence; they can never manufacture a signal on their own.
    signal = trend_aligned and momentum_ok

    # --- Hints ------------------------------------------------------------
    entry_hint = latest.close
    stop_candidate = latest.close - params.atr_stop_mult * atr
    stop_hint = stop_candidate if stop_candidate > 0 else None

    snapshot = {
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "rsi": rsi,
        "atr": atr,
        "volume_z": vol_z,
        "support": levels.support,
        "resistance": levels.resistance,
        "components": {
            "trend_aligned": trend_aligned,
            "momentum_ok": momentum_ok,
            "volume_ok": volume_ok,
            "breakout_confirmed": breakout_confirmed,
        },
        "weights": {
            "trend": params.w_trend,
            "momentum": params.w_momentum,
            "volume": params.w_volume,
            "breakout": params.w_breakout,
        },
    }

    return TASignal(
        symbol=symbol,
        bar_ts=latest.ts,
        signal=signal,
        confidence=confidence,
        breakout_confirmed=breakout_confirmed,
        entry_hint=entry_hint,
        stop_hint=stop_hint,
        support_level=levels.support,
        resistance_level=levels.resistance,
        snapshot=snapshot,
    )
