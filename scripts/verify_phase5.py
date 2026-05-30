r"""Phase 5 green-light — offline verification of the watchlist-filter logic.

Exercises the PURE helpers in core.watchlist_filters (no broker, DB, or
network): the value calculations, each filter's pass/fail boundary, and the
combined verdict — including the key property that a DISABLED filter can never
cause a failure.

Run:
    .\.venv\Scripts\python.exe -m scripts.verify_phase5
"""
from __future__ import annotations

from core.watchlist_filters import (
    FilterParams,
    atr_pct,
    check_liquidity,
    check_spread,
    check_volatility,
    evaluate_filters,
    quote_volume_24h,
    spread_pct,
)

_passed = 0
_failed = 0


def check(name: str, got, want) -> None:
    global _passed, _failed
    if got == want:
        _passed += 1
        print(f"PASS  {name}")
    else:
        _failed += 1
        print(f"FAIL  {name}: got {got!r}, want {want!r}")


def _params(**over) -> FilterParams:
    base = dict(
        liquidity_enabled=True, min_24h_quote_volume=10_000_000.0,
        spread_enabled=True, max_spread_pct=0.20,
        volatility_enabled=True, min_atr_pct=0.5, max_atr_pct=8.0,
        atr_period=14, vol_24h_bars=24, drift_force_exit_enabled=True,
    )
    base.update(over)
    return FilterParams(**base)


def main() -> int:
    # --- value helpers ---
    check("quote_volume_24h sums last N bars",
          quote_volume_24h([10.0] * 5, [2.0] * 5, 3), 60.0)        # 3 * (10*2)
    check("quote_volume_24h None when too few bars",
          quote_volume_24h([10.0, 11.0], [1.0, 1.0], 3), None)
    check("spread_pct mid-based", round(spread_pct(99.0, 101.0), 6), 2.0)  # 2/100
    check("spread_pct None on bad input", spread_pct(0.0, 101.0), None)
    check("atr_pct", round(atr_pct(2.0, 100.0), 6), 2.0)
    check("atr_pct None on bad close", atr_pct(2.0, 0.0), None)

    # --- liquidity boundary ---
    check("liquidity passes at threshold",
          check_liquidity(10_000_000.0, 10_000_000.0).passed, True)
    check("liquidity fails below threshold",
          check_liquidity(9_999_999.0, 10_000_000.0).passed, False)
    check("liquidity fails when None",
          check_liquidity(None, 10_000_000.0).passed, False)

    # --- spread boundary ---
    check("spread passes under cap", check_spread(0.10, 0.20).passed, True)
    check("spread passes at cap", check_spread(0.20, 0.20).passed, True)
    check("spread fails when wide", check_spread(0.50, 0.20).passed, False)
    check("spread fails when no quote", check_spread(None, 0.20).passed, False)

    # --- volatility band (too dead OR too wild both fail) ---
    check("volatility in band passes", check_volatility(2.0, 0.5, 8.0).passed, True)
    check("volatility too dead fails", check_volatility(0.2, 0.5, 8.0).passed, False)
    check("volatility too wild fails", check_volatility(12.0, 0.5, 8.0).passed, False)
    check("volatility at low edge passes", check_volatility(0.5, 0.5, 8.0).passed, True)
    check("volatility at high edge passes", check_volatility(8.0, 0.5, 8.0).passed, True)

    # --- combined evaluate_filters ---
    closes = [100.0] * 30
    liquid_vol = [500_000.0] * 30        # 24 * 100 * 500k = 1.2e9 -> liquid
    p = _params()

    r = evaluate_filters("OK/USD", closes=closes, volumes=liquid_vol,
                         atr_value=2.0, bid=99.99, ask=100.01, params=p)
    check("all filters pass -> passed", r.passed, True)

    r = evaluate_filters("WIDE/USD", closes=closes, volumes=liquid_vol,
                         atr_value=2.0, bid=99.0, ask=101.0, params=p)
    check("wide spread -> overall fail", r.passed, False)

    r = evaluate_filters("THIN/USD", closes=closes, volumes=[1.0] * 30,
                         atr_value=2.0, bid=99.99, ask=100.01, params=p)
    check("thin liquidity -> overall fail", r.passed, False)

    r = evaluate_filters("DEAD/USD", closes=closes, volumes=liquid_vol,
                         atr_value=0.1, bid=99.99, ask=100.01, params=p)
    check("dead volatility -> overall fail", r.passed, False)

    # disabled spread filter must NOT fail even with a terrible spread
    r = evaluate_filters("NOSPREAD/USD", closes=closes, volumes=liquid_vol,
                         atr_value=2.0, bid=1.0, ask=100.0,
                         params=_params(spread_enabled=False))
    check("disabled spread filter is skipped", r.passed, True)

    # all disabled -> trivially passes even with no data
    r = evaluate_filters("NONE/USD", closes=closes, volumes=liquid_vol,
                         atr_value=None, bid=None, ask=None,
                         params=_params(liquidity_enabled=False,
                                        spread_enabled=False,
                                        volatility_enabled=False))
    check("all filters disabled -> passes", r.passed, True)

    print(f"\n{_passed} passed, {_failed} failed.")
    if _failed == 0:
        print("PHASE 5 LOGIC GREEN LIGHT (offline). Live data/DB run still required.")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
