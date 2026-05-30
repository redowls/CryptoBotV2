"""Execution — place paper orders and record ACTUAL fills (SUMMARY §3, §6).

This module is where the structural TA gate lives (CLAUDE.md invariant #1):
:func:`place_entry` takes a required ``ta_signal: bool`` and *refuses to run*
unless it is exactly ``True``. Nothing downstream can buy without a TA signal —
it is a barrier, not a convention.

Two responsibilities, deliberately split so slow broker I/O never holds a DB
transaction open:

  - :func:`place_entry` — submit a market BUY to Alpaca, then poll the order
    until it fills (or terminates), returning a :class:`Fill` with the ACTUAL
    filled qty + average price. Partial fills are normal (invariant #7); we
    report what filled, not what we asked for.
  - :func:`record_position` — write the resulting position row, with BOTH stop
    triggers (price ``sl_price`` and ``support_break_price``), the initial TP,
    confidence-at-entry, the breakout flag, and the effective risk%.

Paper trading only (invariant #10). The caller selects ``paper=True`` until the
full pipeline has run end-to-end on paper.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class Fill:
    """The realized result of an order — ACTUAL fill, not the request."""
    order_id: str
    symbol: str
    requested_qty: float
    filled_qty: float
    avg_price: Optional[float]
    status: str

    @property
    def is_filled(self) -> bool:
        return self.filled_qty > 0 and self.avg_price is not None


def make_trading_client(api_key: str, api_secret: str, *, paper: bool = True):
    """Build an Alpaca TradingClient (paper by default — invariant #10)."""
    from alpaca.trading.client import TradingClient

    return TradingClient(api_key, api_secret, paper=paper)


def _await_fill(client, order, poll_attempts: int, poll_delay: float):
    """Poll an order until it reaches a terminal state or attempts run out."""
    from alpaca.trading.enums import OrderStatus

    terminal = {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
    order_id = str(order.id)
    for attempt in range(poll_attempts):
        if order.status in terminal:
            break
        if attempt < poll_attempts - 1:
            time.sleep(poll_delay)
            order = client.get_order_by_id(order_id)

    filled_qty = float(order.filled_qty or 0)
    avg_price = (
        float(order.filled_avg_price) if order.filled_avg_price is not None else None
    )
    status = getattr(order.status, "value", str(order.status))
    return filled_qty, avg_price, status


def place_entry(
    client,
    *,
    ta_signal: bool,
    symbol: str,
    qty: float,
    time_in_force: str = "gtc",
    poll_attempts: int = 5,
    poll_delay: float = 1.0,
) -> Fill:
    """Submit a market BUY and return the ACTUAL fill.

    The first argument after ``client`` is the non-negotiable gate: this
    function raises ``PermissionError`` unless ``ta_signal is True`` — the
    structural enforcement of CLAUDE.md invariant #1. No TA signal, no order.
    """
    # INVARIANT #1 (structural): TA is the gatekeeper. Refuse, loudly, without
    # a true signal. `is not True` rejects truthy-but-not-True sloppiness too.
    if ta_signal is not True:
        raise PermissionError(
            "place_entry refused: ta_signal gate must be exactly True "
            "(CLAUDE.md invariant #1 — TA is the gatekeeper)."
        )
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")

    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce(time_in_force.lower()),
    )
    order = client.submit_order(order_data=request)
    filled_qty, avg_price, status = _await_fill(
        client, order, poll_attempts, poll_delay
    )
    return Fill(
        order_id=str(order.id),
        symbol=symbol,
        requested_qty=float(qty),
        filled_qty=filled_qty,
        avg_price=avg_price,
        status=status,
    )


def record_position(
    conn: Connection,
    *,
    symbol: str,
    fill: Fill,
    sl_price: float,
    support_break_price: Optional[float],
    tp_price: Optional[float],
    confidence: float,
    breakout_confirmed: bool,
    risk_pct_used: float,
) -> int:
    """Insert the open position row from an actual fill. Returns the new id.

    Records the ACTUAL filled qty + average price (invariant #7) and BOTH stop
    triggers (invariant #5) so the Phase 4 Position Manager never has to guess.
    """
    if not fill.is_filled:
        raise ValueError("record_position called with an unfilled order")

    row = conn.execute(
        text(
            "INSERT INTO dbo.positions "
            "  (symbol, side, qty, entry_price, status, tp_price, sl_price, "
            "   support_break_price, confidence_at_entry, breakout_confirmed, "
            "   risk_pct_used, alpaca_order_id) "
            "OUTPUT INSERTED.id "
            "VALUES (:symbol, 'buy', :qty, :entry, 'open', :tp, :sl, "
            "        :support, :conf, :breakout, :risk, :oid)"
        ),
        {
            "symbol": symbol,
            "qty": fill.filled_qty,
            "entry": fill.avg_price,
            "tp": tp_price,
            "sl": sl_price,
            "support": support_break_price,
            "conf": round(confidence, 2),
            "breakout": 1 if breakout_confirmed else 0,
            "risk": round(risk_pct_used, 2),
            "oid": fill.order_id,
        },
    ).scalar()
    return int(row)
