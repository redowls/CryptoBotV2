"""Position Manager (Phase 4): dynamic TP/SL, trailing, and exits.

For each OPEN position, on the latest CLOSED bar:

1. EXIT check against the levels recorded on the position (set on a PRIOR
   cycle, so no look-ahead). Two independent stop triggers + a TP target,
   first-to-fire wins (invariant #5):
     * price_sl      — a hard protective stop: fires when the bar LOW pierces
                       sl_price (intrabar). Real stop-order semantics; a stop
                       is a trigger, not a guarantee.
     * support_break — a STRUCTURAL exit: fires only on a CONFIRMED CLOSE
                       below the support level (invariant #4 — a wick through
                       the level is NOT a break).
     * tp            — take profit if the bar HIGH reaches the (possibly
                       already-expanded) tp_price.
   On a same-bar conflict between the two stops, the higher trigger price was
   reached first as price fell, so that one wins; protective stops take
   precedence over tp.

2. If no exit, MANAGE FORWARD (these new levels apply from the NEXT bar, never
   the current one — that is what keeps step 1 free of look-ahead):
     * SL trails up:  new_sl = max(old_sl, close - trail_atr_mult * ATR),
       only while in profit. SL NEVER moves backward (invariant #3).
     * Support trails up toward price using the latest PERSISTED sr_levels
       (invariant #6 — read, never recompute here); in favor only.
     * TP expands while trend AND momentum still hold:
       new_tp = max(old_tp, close + tp_r_multiple * (close - new_sl)).

Confidence never enters this file: it scales SIZE at entry only, never the
safety rails here (invariant #3).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from core import config, db
from core.execution import close_position, make_trading_client, record_exit
from core.indicators import atr
from core.levels import latest_levels
from core.market_data import fetch_closed_bars, make_client
from core.ta_engine import TAParams, evaluate

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PMParams:
    trail_atr_mult: float
    atr_period: int
    tp_r_multiple: float
    tp_expand_requires_alignment: bool
    trail_only_in_profit: bool
    support_trail_enabled: bool

    @staticmethod
    def from_config() -> "PMParams":
        return PMParams(
            trail_atr_mult=config.get_float("pm.trail_atr_mult", 1.5),
            atr_period=config.get_int("ta.atr_period", 14),
            tp_r_multiple=config.get_float("pm.tp_r_multiple", 2.0),
            tp_expand_requires_alignment=config.get_bool(
                "pm.tp_expand_requires_alignment", True
            ),
            trail_only_in_profit=config.get_bool("pm.trail_only_in_profit", True),
            support_trail_enabled=config.get_bool("pm.support_trail_enabled", True),
        )


@dataclass
class ExitDecision:
    """The exit verdict for a single bar. `reason is None` => hold."""

    reason: str | None  # 'price_sl' | 'support_break' | 'tp' | None
    trigger_price: float | None = None


@dataclass
class ExitResult:
    position_id: int
    symbol: str
    reason: str
    exit_price: float | None
    dry_run: bool = False


# ----------------------------------------------------------------------------
# Pure decision helpers (no I/O — directly unit-testable, see scripts/verify_phase4)
# ----------------------------------------------------------------------------


def resolve_exit(
    *,
    bar_low: float,
    bar_high: float,
    bar_close: float,
    sl_price: float | None,
    support_price: float | None,
    tp_price: float | None,
) -> ExitDecision:
    """First-to-fire exit verdict for one closed bar (invariants #4, #5).

    * price_sl: bar LOW pierces sl_price (intrabar hard stop).
    * support_break: CONFIRMED CLOSE strictly below support (wick != break).
    * tp: bar HIGH reaches tp_price.
    Protective stops beat tp; between the two stops the higher level fired
    first as price descended.
    """
    sl_hit = sl_price is not None and bar_low <= sl_price
    support_hit = support_price is not None and bar_close < support_price
    tp_hit = tp_price is not None and bar_high >= tp_price

    if sl_hit and support_hit:
        if support_price >= sl_price:  # support is higher -> reached first
            return ExitDecision("support_break", support_price)
        return ExitDecision("price_sl", sl_price)
    if sl_hit:
        return ExitDecision("price_sl", sl_price)
    if support_hit:
        return ExitDecision("support_break", support_price)
    if tp_hit:
        return ExitDecision("tp", tp_price)
    return ExitDecision(None, None)


def trail_sl(
    *,
    current_sl: float,
    close: float,
    atr_value: float | None,
    mult: float,
    entry: float,
    only_in_profit: bool,
) -> float:
    """Ratchet the SL up; never down (invariant #3). Returns the new SL."""
    if atr_value is None:
        return current_sl
    if only_in_profit and close <= entry:
        return current_sl
    candidate = close - mult * atr_value
    return max(current_sl, candidate)


def trail_support(
    *,
    current_support: float | None,
    persisted_support: float | None,
    close: float,
) -> float | None:
    """Trail the support trigger up toward price, in favor only.

    Uses the latest PERSISTED level (invariant #6). Never set support at or
    above the current close (that would trigger instantly).
    """
    if persisted_support is None or persisted_support >= close:
        return current_support
    if current_support is None:
        return persisted_support
    return max(current_support, persisted_support)


def expand_tp(
    *,
    current_tp: float | None,
    close: float,
    new_sl: float,
    r_multiple: float,
    aligned: bool,
    requires_alignment: bool,
) -> float | None:
    """Expand the TP target upward while the trend holds; never shrink it."""
    if requires_alignment and not aligned:
        return current_tp
    candidate = close + r_multiple * (close - new_sl)
    if current_tp is None:
        return candidate
    return max(current_tp, candidate)


# ----------------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------------


def _load_open_positions(conn) -> list[dict]:
    rows = conn.execute(
        db.text(
            "SELECT id, symbol, qty, entry_price, sl_price, "
            "support_break_price, tp_price, entry_ts_utc "
            "FROM dbo.positions WHERE status = 'open'"
        )
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _apply_adjustments(
    conn,
    *,
    position_id: int,
    symbol: str,
    bar_ts_utc,
    new_sl: float,
    new_support: float | None,
    new_tp: float | None,
    changes: list[tuple[str, float | None, float]],
) -> None:
    """Persist the new trigger levels + one audit row per change."""
    conn.execute(
        db.text(
            "UPDATE dbo.positions SET sl_price = :sl, "
            "support_break_price = :support, tp_price = :tp WHERE id = :pid"
        ),
        {"sl": new_sl, "support": new_support, "tp": new_tp, "pid": position_id},
    )
    for adj_type, old_val, new_val in changes:
        conn.execute(
            db.text(
                "INSERT INTO dbo.position_adjustments "
                "(position_id, symbol, adjustment_type, old_value, new_value, "
                " bar_ts_utc) "
                "VALUES (:pid, :symbol, :type, :old, :new, :bar_ts)"
            ),
            {
                "pid": position_id,
                "symbol": symbol,
                "type": adj_type,
                "old": old_val,
                "new": new_val,
                "bar_ts": bar_ts_utc,
            },
        )


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------


def manage_open_positions(
    *,
    symbols: list[str] | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> list[ExitResult]:
    """Manage every open position on its latest closed bar.

    Broker I/O (the liquidation order) is kept OUT of the DB transaction, like
    the entry path. Returns the list of exits taken (or simulated in dry-run).
    """
    now = now or _utcnow()
    cfg_tf = config.get_str("scan.timeframe", "1Hour")
    bars_to_fetch = config.get_int("scan.bars_to_fetch", 250)
    pm = PMParams.from_config()
    ta_params = TAParams.from_config()

    with db.connect_with_retry() as conn:
        positions = _load_open_positions(conn)

    if not positions:
        logger.info("manage: no open positions")
        return []

    key, secret = db.get_active_credentials("alpaca", "paper")
    data_client = make_client(key, secret)
    trading_client = make_trading_client(key, secret, paper=True)

    results: list[ExitResult] = []

    for pos in positions:
        symbol = pos["symbol"]
        if symbols and symbol not in symbols:
            continue

        try:
            bars = fetch_closed_bars(
                data_client, symbol, timeframe=cfg_tf, limit=bars_to_fetch, now=now
            )
        except Exception:
            logger.exception("manage: fetch bars failed for %s", symbol)
            continue
        if not bars:
            logger.warning("manage: no closed bars for %s", symbol)
            continue

        latest = bars[-1]
        entry_price = float(pos["entry_price"])
        sl_price = float(pos["sl_price"])
        support_price = (
            float(pos["support_break_price"])
            if pos["support_break_price"] is not None
            else None
        )
        tp_price = float(pos["tp_price"]) if pos["tp_price"] is not None else None

        # --- 1) EXIT check against the CURRENTLY recorded levels ---
        decision = resolve_exit(
            bar_low=latest.low,
            bar_high=latest.high,
            bar_close=latest.close,
            sl_price=sl_price,
            support_price=support_price,
            tp_price=tp_price,
        )
        if decision.reason is not None:
            if dry_run:
                logger.info(
                    "DRY-RUN exit %s pos#%s reason=%s trigger~%.6f close=%.6f",
                    symbol, pos["id"], decision.reason,
                    decision.trigger_price, latest.close,
                )
                results.append(
                    ExitResult(pos["id"], symbol, decision.reason, None, dry_run=True)
                )
                continue
            try:
                fill = close_position(
                    trading_client,
                    symbol,
                    poll_attempts=config.get_int("execution.poll_attempts", 5),
                    poll_delay=config.get_float("execution.poll_delay_secs", 1.0),
                )
            except Exception:
                logger.exception("manage: close failed for %s", symbol)
                continue

            exit_price = (
                fill.avg_price
                if fill.avg_price is not None and fill.avg_price > 0
                else latest.close
            )
            exit_qty = fill.filled_qty if fill.filled_qty > 0 else float(pos["qty"])
            with db.connect_with_retry() as conn:
                with conn.begin():
                    record_exit(
                        conn,
                        position_id=pos["id"],
                        symbol=symbol,
                        qty=exit_qty,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        opened_at_utc=pos["entry_ts_utc"],
                        exit_reason=decision.reason,
                    )
            logger.info(
                "exited %s pos#%s reason=%s qty=%.9f exit=%.6f pnl=%.4f",
                symbol, pos["id"], decision.reason, exit_qty, exit_price,
                (exit_price - entry_price) * exit_qty,
            )
            results.append(ExitResult(pos["id"], symbol, decision.reason, exit_price))
            continue

        # --- 2) MANAGE FORWARD: trail SL/support + expand TP (next bar) ---
        atr_value = atr(
            [b.high for b in bars],
            [b.low for b in bars],
            [b.close for b in bars],
            pm.atr_period,
        )
        sig = evaluate(symbol, bars, ta_params)
        components = sig.snapshot.get("components", {})
        aligned = bool(
            components.get("trend_aligned") and components.get("momentum_ok")
        )

        with db.connect_with_retry() as conn:
            persisted = latest_levels(conn, symbol)  # invariant #6
        persisted_support = persisted.support if persisted else None

        new_sl = trail_sl(
            current_sl=sl_price,
            close=latest.close,
            atr_value=atr_value,
            mult=pm.trail_atr_mult,
            entry=entry_price,
            only_in_profit=pm.trail_only_in_profit,
        )
        new_support = (
            trail_support(
                current_support=support_price,
                persisted_support=persisted_support,
                close=latest.close,
            )
            if pm.support_trail_enabled
            else support_price
        )
        new_tp = expand_tp(
            current_tp=tp_price,
            close=latest.close,
            new_sl=new_sl,
            r_multiple=pm.tp_r_multiple,
            aligned=aligned,
            requires_alignment=pm.tp_expand_requires_alignment,
        )

        changes: list[tuple[str, float | None, float]] = []
        if new_sl > sl_price:
            changes.append(("sl_trail", sl_price, new_sl))
        if new_support is not None and (
            support_price is None or new_support > support_price
        ):
            changes.append(("support_trail", support_price, new_support))
        if new_tp is not None and (tp_price is None or new_tp > tp_price):
            changes.append(("tp_expand", tp_price, new_tp))

        if not changes:
            logger.info("manage %s pos#%s: no change", symbol, pos["id"])
            continue

        if dry_run:
            logger.info(
                "DRY-RUN manage %s pos#%s: %s",
                symbol, pos["id"],
                ", ".join(f"{t}:{o}->{n}" for t, o, n in changes),
            )
            continue

        with db.connect_with_retry() as conn:
            with conn.begin():
                _apply_adjustments(
                    conn,
                    position_id=pos["id"],
                    symbol=symbol,
                    bar_ts_utc=latest.ts,
                    new_sl=new_sl,
                    new_support=new_support,
                    new_tp=new_tp,
                    changes=changes,
                )
        logger.info(
            "managed %s pos#%s: %s",
            symbol, pos["id"],
            ", ".join(f"{t}:{o}->{n}" for t, o, n in changes),
        )

    return results
