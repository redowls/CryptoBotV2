"""Watchlist filters (Phase 5): the tradeable-universe gate.

Three independent quality filters decide whether a watchlist symbol is eligible
for a NEW entry on this bar. They gate ENTRIES only — open positions are ALWAYS
managed for exits regardless of filters, so a symbol going illiquid can never
strand an open position unmanaged (SUMMARY §10 "wallet vs watchlist drift").

  * liquidity   — 24h quote volume (sum of close*volume over the last N closed
                  1h bars) must clear a floor. A thin book can't be exited well.
  * spread      — the LIVE bid/ask spread % must be under a cap. A wide spread
                  is an immediate, guaranteed cost on BOTH entry and exit.
  * volatility  — ATR as a % of price must sit inside a band: too DEAD (no
                  movement → no edge, fees dominate) or too WILD (stops get run)
                  are both skipped.

Each filter is individually toggleable in app_config (``filters.*``); every
value is tunable in SQL without a code change, like every other strategy knob
(invariant: never hard-code a tunable).

The pure ``*_pct`` / ``check_*`` / ``evaluate_filters`` helpers take plain
numbers and are side-effect-free, so they can be verified offline
(``scripts/verify_phase5``), exactly like the Phase 4 decision helpers.

Phase 5 also owns the wallet-vs-watchlist DRIFT safety
(:func:`enforce_watchlist_drift`): if an open position's symbol has been
deactivated / removed from the watchlist AND the position is underwater,
force-exit it (``exit_reason='manual'``) rather than leaving it to drift
unmanaged. An in-profit deactivated position is left to the Position Manager's
normal trailing exit — deactivation only blocks NEW entries.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from core import config, db
from core.execution import close_position, make_trading_client, record_exit
from core.indicators import atr
from core.market_data import fetch_closed_bars, latest_quote, make_client

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------------
# Params
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class FilterParams:
    liquidity_enabled: bool
    min_24h_quote_volume: float
    spread_enabled: bool
    max_spread_pct: float
    volatility_enabled: bool
    min_atr_pct: float
    max_atr_pct: float
    atr_period: int
    vol_24h_bars: int
    drift_force_exit_enabled: bool

    @staticmethod
    def from_config() -> "FilterParams":
        return FilterParams(
            liquidity_enabled=config.get_bool("filters.liquidity_enabled", True),
            min_24h_quote_volume=config.get_float(
                "filters.min_24h_quote_volume", 10_000_000.0
            ),
            spread_enabled=config.get_bool("filters.spread_enabled", True),
            max_spread_pct=config.get_float("filters.max_spread_pct", 0.20),
            volatility_enabled=config.get_bool("filters.volatility_enabled", True),
            min_atr_pct=config.get_float("filters.min_atr_pct", 0.5),
            max_atr_pct=config.get_float("filters.max_atr_pct", 8.0),
            # The volatility filter reuses the TA engine's ATR period so the
            # ATR a position is sized against is the ATR it was screened on.
            atr_period=config.get_int("ta.atr_period", 14),
            vol_24h_bars=config.get_int("filters.vol_24h_bars", 24),
            drift_force_exit_enabled=config.get_bool(
                "filters.drift_force_exit_enabled", True
            ),
        )


@dataclass(frozen=True)
class FilterCheck:
    """One filter's outcome for one symbol."""

    name: str
    passed: bool
    value: Optional[float]
    threshold: str
    detail: str = ""


@dataclass(frozen=True)
class FilterResult:
    symbol: str
    passed: bool          # ALL enabled filters passed
    checks: list[FilterCheck]

    def summary(self) -> str:
        if self.passed:
            return "PASS"
        return "FAIL:" + ",".join(c.name for c in self.checks if not c.passed)


# ----------------------------------------------------------------------------
# Pure value helpers (no I/O — directly unit-testable, see scripts/verify_phase5)
# ----------------------------------------------------------------------------
def quote_volume_24h(
    closes: Sequence[float], volumes: Sequence[float], n_bars: int
) -> Optional[float]:
    """Sum of close*volume over the last `n_bars` bars (quote-currency volume).

    Alpaca reports base-asset volume; multiplying by close converts it to quote
    (e.g. USD) so the threshold is a dollar figure. Returns None if there are
    fewer than `n_bars` bars (can't assess a full 24h → caller treats as fail).
    """
    if n_bars <= 0:
        raise ValueError("n_bars must be positive")
    if len(closes) != len(volumes):
        raise ValueError("closes and volumes must be the same length")
    if len(closes) < n_bars:
        return None
    return sum(c * v for c, v in zip(closes[-n_bars:], volumes[-n_bars:]))


def spread_pct(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """Bid/ask spread as a % of the mid price. None on missing/invalid quote."""
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 100.0


def atr_pct(atr_value: Optional[float], close: Optional[float]) -> Optional[float]:
    """ATR as a % of price. None when ATR or price is unavailable."""
    if atr_value is None or close is None or close <= 0:
        return None
    return atr_value / close * 100.0


# ----------------------------------------------------------------------------
# Pure per-filter checks
# ----------------------------------------------------------------------------
def check_liquidity(quote_vol: Optional[float], minimum: float) -> FilterCheck:
    passed = quote_vol is not None and quote_vol >= minimum
    detail = (
        "insufficient bars"
        if quote_vol is None
        else f"24h quote vol {quote_vol:,.0f}"
    )
    return FilterCheck("liquidity", passed, quote_vol, f">= {minimum:,.0f}", detail)


def check_spread(sp: Optional[float], cap: float) -> FilterCheck:
    passed = sp is not None and sp <= cap
    detail = "no quote" if sp is None else f"spread {sp:.3f}%"
    return FilterCheck("spread", passed, sp, f"<= {cap:.3f}%", detail)


def check_volatility(ap: Optional[float], lo: float, hi: float) -> FilterCheck:
    passed = ap is not None and lo <= ap <= hi
    detail = "no ATR" if ap is None else f"ATR {ap:.2f}%"
    return FilterCheck("volatility", passed, ap, f"{lo:.2f}-{hi:.2f}%", detail)


def evaluate_filters(
    symbol: str,
    *,
    closes: Sequence[float],
    volumes: Sequence[float],
    atr_value: Optional[float],
    bid: Optional[float],
    ask: Optional[float],
    params: FilterParams,
) -> FilterResult:
    """Combine the enabled filters into a single verdict (pure).

    A DISABLED filter contributes no check, so it can never cause a fail — that
    is the property the offline test pins down.
    """
    checks: list[FilterCheck] = []
    if params.liquidity_enabled:
        qv = quote_volume_24h(closes, volumes, params.vol_24h_bars)
        checks.append(check_liquidity(qv, params.min_24h_quote_volume))
    if params.spread_enabled:
        checks.append(check_spread(spread_pct(bid, ask), params.max_spread_pct))
    if params.volatility_enabled:
        latest_close = closes[-1] if closes else None
        ap = atr_pct(atr_value, latest_close)
        checks.append(check_volatility(ap, params.min_atr_pct, params.max_atr_pct))
    passed = all(c.passed for c in checks)
    return FilterResult(symbol, passed, checks)


# ----------------------------------------------------------------------------
# Orchestration helper (fetches the live quote when the spread filter is on)
# ----------------------------------------------------------------------------
def screen_symbol(data_client, symbol: str, bars, params: FilterParams) -> FilterResult:
    """Run the entry-eligibility filters for one symbol on its fetched bars.

    A live quote is fetched ONLY when the spread filter is enabled (one API
    call). A quote-fetch failure is conservatively treated as a spread FAIL —
    we will not enter a book we cannot currently price.
    """
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    atr_value = atr(highs, lows, closes, params.atr_period)

    bid: Optional[float] = None
    ask: Optional[float] = None
    if params.spread_enabled:
        try:
            q = latest_quote(data_client, symbol)
            if q is not None:
                bid, ask = q.bid, q.ask
        except Exception:
            logger.exception("screen: quote fetch failed for %s", symbol)
            # bid/ask stay None -> spread check fails (conservative)

    return evaluate_filters(
        symbol,
        closes=closes,
        volumes=volumes,
        atr_value=atr_value,
        bid=bid,
        ask=ask,
        params=params,
    )


# ----------------------------------------------------------------------------
# Wallet-vs-watchlist drift safety (SUMMARY §10)
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class DriftExit:
    position_id: int
    symbol: str
    action: str               # 'force_exit' | 'kept'
    exit_price: Optional[float] = None
    dry_run: bool = False


def _active_watchlist_symbols(conn) -> set[str]:
    rows = conn.execute(
        db.text("SELECT symbol FROM dbo.watchlist WHERE is_active = 1")
    ).fetchall()
    return {r[0] for r in rows}


def _open_positions(conn) -> list[dict]:
    rows = conn.execute(
        db.text(
            "SELECT id, symbol, qty, entry_price, entry_ts_utc "
            "FROM dbo.positions WHERE status = 'open'"
        )
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def enforce_watchlist_drift(
    *, dry_run: bool = False, now: Optional[datetime] = None
) -> list[DriftExit]:
    """Force-exit open positions whose symbol left the watchlist AND are losing.

    The hazard (SUMMARY §10): a symbol is deactivated/removed from the watchlist
    while a position is still open. The entry pass already skips it, but the
    position itself must not silently drift. Rule (the user's choice): if it is
    UNDERWATER (latest close < entry), force-exit now via the same broker path
    as a manual flatten (``exit_reason='manual'``); if it is flat/in-profit,
    leave it to the Position Manager's normal trailing exit.

    Broker I/O is kept OUT of the DB transaction, like every other exit path.
    Returns the list of drift actions taken (or simulated in dry-run).
    """
    params = FilterParams.from_config()
    if not params.drift_force_exit_enabled:
        return []
    now = now or _utcnow()
    cfg_tf = config.get_str("scan.timeframe", "1Hour")
    bars_to_fetch = config.get_int("scan.bars_to_fetch", 250)
    environment = config.get_str("environment", "paper")

    with db.connect_with_retry() as conn:
        positions = _open_positions(conn)
        active = _active_watchlist_symbols(conn)

    drifted = [p for p in positions if p["symbol"] not in active]
    if not drifted:
        return []

    key, secret = db.get_active_credentials("alpaca", environment)
    data_client = make_client(key, secret)
    trading_client = make_trading_client(key, secret, paper=(environment != "live"))

    results: list[DriftExit] = []
    for pos in drifted:
        symbol = pos["symbol"]
        entry_price = float(pos["entry_price"])
        try:
            bars = fetch_closed_bars(
                data_client, symbol, timeframe=cfg_tf, limit=bars_to_fetch, now=now
            )
        except Exception:
            logger.exception("drift: fetch bars failed for %s", symbol)
            continue
        if not bars:
            logger.warning("drift: no closed bars for %s - cannot assess", symbol)
            continue

        close = bars[-1].close
        if close >= entry_price:
            logger.info(
                "drift %s pos#%s: deactivated but not underwater "
                "(close=%.6f >= entry=%.6f) - left to PM",
                symbol, pos["id"], close, entry_price,
            )
            results.append(DriftExit(pos["id"], symbol, "kept", None, dry_run))
            continue

        if dry_run:
            logger.info(
                "DRY-RUN drift force-exit %s pos#%s underwater close=%.6f entry=%.6f",
                symbol, pos["id"], close, entry_price,
            )
            results.append(
                DriftExit(pos["id"], symbol, "force_exit", None, dry_run=True)
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
            logger.exception("drift: close failed for %s", symbol)
            continue

        exit_price = (
            fill.avg_price
            if fill.avg_price is not None and fill.avg_price > 0
            else close
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
                    exit_reason="manual",
                )
        logger.info(
            "drift force-exit %s pos#%s exit=%.6f pnl=%.4f (deactivated + underwater)",
            symbol, pos["id"], exit_price, (exit_price - entry_price) * exit_qty,
        )
        results.append(DriftExit(pos["id"], symbol, "force_exit", exit_price))

    return results
