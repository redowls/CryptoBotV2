r"""Phase 4 green-light — offline verification of the Position Manager logic.

Exercises the PURE decision functions in core.position_manager (no broker, no
DB, no network) so the invariants can be checked deterministically:

  * price-SL hit (intrabar low pierces sl_price)        -> 'price_sl'
  * confirmed CLOSE below support                        -> 'support_break'
  * a single WICK below support (low pierces, close above) does NOT trigger
  * TP hit (intrabar high reaches tp_price)              -> 'tp'
  * first-to-fire when both stops hit the same bar       -> higher trigger wins
  * SL trails UP in profit, never backward               (invariant #3)
  * SL does NOT trail while underwater (only_in_profit)
  * TP expands only while trend+momentum aligned; never shrinks
  * support trails up toward price, in favor only        (invariant #6 source)

Run:
    .\.venv\Scripts\python.exe -m scripts.verify_phase4
"""
from __future__ import annotations

from core.position_manager import (
    expand_tp,
    resolve_exit,
    trail_sl,
    trail_support,
)

_passed = 0
_failed = 0


def check(name: str, got, want) -> None:
    global _passed, _failed
    ok = got == want
    if ok:
        _passed += 1
        print(f"PASS  {name}")
    else:
        _failed += 1
        print(f"FAIL  {name}: got {got!r}, want {want!r}")


def main() -> int:
    # --- EXIT resolution (invariants #4, #5) ---
    # Price-SL: bar low pierces sl_price.
    d = resolve_exit(bar_low=99.0, bar_high=105.0, bar_close=104.0,
                     sl_price=100.0, support_price=90.0, tp_price=120.0)
    check("price_sl on low pierce", d.reason, "price_sl")

    # Confirmed close below support => support_break.
    d = resolve_exit(bar_low=85.0, bar_high=95.0, bar_close=89.0,
                     sl_price=80.0, support_price=90.0, tp_price=120.0)
    check("support_break on confirmed close", d.reason, "support_break")

    # WICK below support (low dips under, close back above) => NO break.
    d = resolve_exit(bar_low=88.0, bar_high=96.0, bar_close=92.0,
                     sl_price=80.0, support_price=90.0, tp_price=120.0)
    check("wick below support does NOT trigger", d.reason, None)

    # TP hit: bar high reaches tp_price (no stop hit).
    d = resolve_exit(bar_low=101.0, bar_high=121.0, bar_close=119.0,
                     sl_price=100.0, support_price=90.0, tp_price=120.0)
    check("tp on high reach", d.reason, "tp")

    # Both stops hit same bar: support (95) above price-SL (90) => support first.
    d = resolve_exit(bar_low=88.0, bar_high=100.0, bar_close=94.0,
                     sl_price=90.0, support_price=95.0, tp_price=120.0)
    check("first-to-fire: higher support wins", d.reason, "support_break")

    # Both hit, price-SL (95) above support (90) => price_sl first.
    d = resolve_exit(bar_low=88.0, bar_high=100.0, bar_close=89.0,
                     sl_price=95.0, support_price=90.0, tp_price=120.0)
    check("first-to-fire: higher price-SL wins", d.reason, "price_sl")

    # Protective stop beats TP if both somehow flagged.
    d = resolve_exit(bar_low=99.0, bar_high=121.0, bar_close=110.0,
                     sl_price=100.0, support_price=None, tp_price=120.0)
    check("stop precedence over tp", d.reason, "price_sl")

    # --- SL trailing (invariant #3) ---
    # In profit: trails up.
    new = trail_sl(current_sl=90.0, close=110.0, atr_value=4.0, mult=1.5,
                   entry=100.0, only_in_profit=True)
    check("SL trails up in profit", new, 104.0)  # 110 - 1.5*4

    # In profit but candidate below current SL: never moves backward.
    new = trail_sl(current_sl=106.0, close=110.0, atr_value=4.0, mult=1.5,
                   entry=100.0, only_in_profit=True)
    check("SL never moves backward", new, 106.0)

    # Underwater + only_in_profit: SL frozen.
    new = trail_sl(current_sl=90.0, close=98.0, atr_value=4.0, mult=1.5,
                   entry=100.0, only_in_profit=True)
    check("SL frozen while underwater", new, 90.0)

    # --- TP expansion ---
    # Aligned: expands up.
    new = expand_tp(current_tp=120.0, close=110.0, new_sl=104.0, r_multiple=2.0,
                    aligned=True, requires_alignment=True)
    check("TP expands when aligned", new, 122.0)  # 110 + 2*(110-104)

    # Not aligned + requires_alignment: TP unchanged.
    new = expand_tp(current_tp=120.0, close=130.0, new_sl=104.0, r_multiple=2.0,
                    aligned=False, requires_alignment=True)
    check("TP frozen when not aligned", new, 120.0)

    # Never shrinks: candidate below current TP.
    new = expand_tp(current_tp=200.0, close=110.0, new_sl=104.0, r_multiple=2.0,
                    aligned=True, requires_alignment=True)
    check("TP never shrinks", new, 200.0)

    # --- Support trailing (in favor only) ---
    new = trail_support(current_support=90.0, persisted_support=95.0, close=110.0)
    check("support trails up", new, 95.0)

    new = trail_support(current_support=95.0, persisted_support=92.0, close=110.0)
    check("support never trails down", new, 95.0)

    # Persisted >= close would trigger instantly => ignore it.
    new = trail_support(current_support=90.0, persisted_support=115.0, close=110.0)
    check("support not set at/above close", new, 90.0)

    print(f"\n{_passed} passed, {_failed} failed.")
    if _failed == 0:
        print("PHASE 4 LOGIC GREEN LIGHT (offline). Live broker/DB run still required.")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
