"""Position sizing — the locked formula (SUMMARY §4, CLAUDE.md invariant #3).

Confidence scales SIZE only, never the safety rails. The per-unit risk
``R = entry - stop`` is fixed by the TA engine's price-based stop; confidence
just decides how many R the account is willing to wager, between the risk
profile's min and max per-trade percentages:

    risk_pct(confidence) = lerp(min_risk_per_trade_pct, max_risk_per_trade_pct,
                                normalize(confidence, min_confidence, 100))
    qty = (equity * risk_pct) / abs(entry - stop)

The raw qty is then clamped by two independent ceilings, either of which can
shrink (or veto) the trade — never enlarge it:

  1. **Portfolio risk** — aggregate open risk may not exceed
     ``max_portfolio_risk_pct`` of equity (correlated-drawdown guard), and no
     more than ``max_open_positions`` may be open at once.
  2. **Spendable cash** — a tight stop + high confidence + large equity can ask
     for more notional than the account holds; clamp to available cash.

Everything here is pure given a :class:`RiskProfile`; the only I/O is
:func:`load_active_risk_profile`, which reads the single active row of the
``risk_profile`` table (tunable in SQL without code changes).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Crypto allows fractional qty; round DOWN to this many decimals so a clamp can
# never round us back ABOVE a cash/risk ceiling.
QTY_DECIMALS = 9


@dataclass(frozen=True)
class RiskProfile:
    """The fixed risk envelope (the active ``risk_profile`` row)."""
    name: str
    max_risk_per_trade_pct: float   # risk% at top confidence (100)
    min_risk_per_trade_pct: float   # risk% at the confidence floor
    min_confidence_to_trade: float  # below this => no trade
    max_portfolio_risk_pct: float   # aggregate open-risk ceiling
    max_open_positions: int         # simultaneous position cap


@dataclass(frozen=True)
class SizeResult:
    """Outcome of a sizing attempt. ``accepted=False`` => do not trade."""
    accepted: bool
    qty: float                 # ACTUAL qty to request (base units), after clamps
    risk_pct_used: float       # EFFECTIVE risk% after clamps (recorded on the position)
    notional: float            # qty * entry (quote currency)
    reason: str                # human-readable accept/reject explanation


def load_active_risk_profile(conn: Connection) -> RiskProfile:
    """Read the single active risk profile. Raises if none is active."""
    row = conn.execute(
        text(
            "SELECT TOP 1 name, max_risk_per_trade_pct, min_risk_per_trade_pct, "
            "       min_confidence_to_trade, max_portfolio_risk_pct, max_open_positions "
            "FROM dbo.risk_profile WHERE is_active = 1"
        )
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "No active risk_profile. Seed one and set is_active = 1."
        )
    return RiskProfile(
        name=row[0],
        max_risk_per_trade_pct=float(row[1]),
        min_risk_per_trade_pct=float(row[2]),
        min_confidence_to_trade=float(row[3]),
        max_portfolio_risk_pct=float(row[4]),
        max_open_positions=int(row[5]),
    )


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _normalize(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 1.0
    return _clamp((value - lo) / (hi - lo), 0.0, 1.0)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _round_down(x: float, decimals: int) -> float:
    factor = 10 ** decimals
    return math.floor(x * factor) / factor


def risk_pct_for_confidence(confidence: float, profile: RiskProfile) -> float:
    """Interpolate per-trade risk% from confidence (floor -> min, 100 -> max)."""
    t = _normalize(confidence, profile.min_confidence_to_trade, 100.0)
    return _lerp(profile.min_risk_per_trade_pct, profile.max_risk_per_trade_pct, t)


def compute_size(
    *,
    confidence: float,
    equity: float,
    cash_available: float,
    entry_price: float,
    stop_price: Optional[float],
    profile: RiskProfile,
    open_position_count: int,
    current_open_risk: float,
    min_notional: float = 0.0,
) -> SizeResult:
    """Size a long entry per the locked formula, clamped by portfolio + cash.

    ``current_open_risk`` is the aggregate quote-currency risk already committed
    across open positions (sum of ``qty * (entry - sl)``). ``min_notional``
    rejects dust orders. Returns ``accepted=False`` with a reason on any veto.
    """
    def reject(reason: str) -> SizeResult:
        return SizeResult(False, 0.0, 0.0, 0.0, reason)

    if confidence < profile.min_confidence_to_trade:
        return reject(
            f"confidence {confidence:.1f} < floor {profile.min_confidence_to_trade:.0f}"
        )
    if open_position_count >= profile.max_open_positions:
        return reject(f"max open positions ({profile.max_open_positions}) reached")
    if equity <= 0:
        return reject(f"equity {equity:.2f} <= 0")
    if cash_available <= 0:
        return reject(f"no spendable cash ({cash_available:.2f})")
    if stop_price is None or stop_price <= 0 or stop_price >= entry_price:
        return reject(f"invalid stop {stop_price} vs entry {entry_price:.2f}")

    per_unit_risk = entry_price - stop_price

    # 1. Confidence -> risk dollars.
    risk_pct = risk_pct_for_confidence(confidence, profile)
    risk_dollars = equity * (risk_pct / 100.0)

    # 2. Portfolio aggregate-risk ceiling.
    portfolio_budget = equity * (profile.max_portfolio_risk_pct / 100.0)
    remaining_budget = portfolio_budget - current_open_risk
    if remaining_budget <= 0:
        return reject(
            f"portfolio risk budget exhausted "
            f"(open {current_open_risk:.2f} >= cap {portfolio_budget:.2f})"
        )
    risk_dollars = min(risk_dollars, remaining_budget)

    qty = risk_dollars / per_unit_risk

    # 3. Spendable-cash ceiling.
    if qty * entry_price > cash_available:
        qty = cash_available / entry_price

    qty = _round_down(qty, QTY_DECIMALS)
    notional = qty * entry_price

    if qty <= 0:
        return reject("computed qty rounds to 0 after clamps")
    if notional < min_notional:
        return reject(
            f"notional {notional:.2f} < min_notional {min_notional:.2f}"
        )

    # Record the EFFECTIVE risk% actually committed (post-clamp), not the
    # confidence-implied one — that is what landed on the position.
    effective_risk_pct = (qty * per_unit_risk) / equity * 100.0

    return SizeResult(
        accepted=True,
        qty=qty,
        risk_pct_used=effective_risk_pct,
        notional=notional,
        reason="ok",
    )
