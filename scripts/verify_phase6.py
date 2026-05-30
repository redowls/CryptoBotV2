r"""Phase 6 green-light — offline verification of the AI confidence-multiplier logic.

Exercises the PURE helpers in core.ai_client (no Anthropic SDK, no broker, DB,
or network): the clamp, the product-combine with its dampening floor, the
JSON-reply parser's fail-open behaviour, and — most importantly — the structural
guarantee of invariant #2:

    * the AI can only SUBTRACT (adjusted confidence <= TA confidence, ALWAYS);
    * a multiplier > 1 is clamped to 1 (the AI can never RAISE confidence); and
    * the TA `signal` bool is passed through UNCHANGED, so the AI can never
      manufacture an entry the TA engine did not approve.

Run:
    .\.venv\Scripts\python.exe -m scripts.verify_phase6
"""
from __future__ import annotations

from core.ai_client import (
    apply_ai_multiplier,
    clamp01,
    combine_multipliers,
    parse_multiplier,
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


def check_true(name: str, cond: bool) -> None:
    check(name, bool(cond), True)


def main() -> int:
    # --- clamp01 ---
    check("clamp01 below 0 -> 0", clamp01(-0.5), 0.0)
    check("clamp01 above 1 -> 1", clamp01(1.5), 1.0)
    check("clamp01 in range unchanged", clamp01(0.37), 0.37)

    # --- combine_multipliers: product, each can only subtract ---
    check("combine empty -> 1.0", combine_multipliers([]), 1.0)
    check("combine product", round(combine_multipliers([0.5, 0.5]), 6), 0.25)
    check("combine clamps inputs >1", round(combine_multipliers([2.0, 0.5]), 6), 0.5)
    check("combine clamps inputs <0", combine_multipliers([-1.0, 0.5]), 0.0)
    check_true(
        "combine result never exceeds any clamped input",
        combine_multipliers([0.8, 0.9]) <= 0.8 + 1e-9,
    )
    # min_multiplier FLOOR bounds how far AI may dampen
    check("combine respects floor", round(combine_multipliers([0.0, 0.0], floor=0.5), 6), 0.5)
    check("combine floor clamped to 1", combine_multipliers([0.0], floor=2.0), 1.0)

    # --- apply_ai_multiplier: invariant #2 ---
    sig, adj = apply_ai_multiplier(80.0, 0.5, signal=True)
    check("apply: signal passes through (True)", sig, True)
    check("apply: dampens confidence", adj, 40.0)

    # AI can NEVER raise: multiplier > 1 is clamped, adjusted == ta_confidence
    _, adj = apply_ai_multiplier(80.0, 5.0, signal=True)
    check("apply: multiplier>1 cannot raise confidence", adj, 80.0)

    # AI can veto: multiplier 0 -> 0 confidence
    _, adj = apply_ai_multiplier(80.0, 0.0, signal=True)
    check("apply: multiplier 0 vetoes (conf 0)", adj, 0.0)

    # adjusted <= ta_confidence for a sweep of multipliers (the core property)
    monotone_ok = all(
        apply_ai_multiplier(75.0, m / 10.0, signal=True)[1] <= 75.0 + 1e-9
        for m in range(0, 31)  # multipliers 0.0 .. 3.0, incl. >1
    )
    check_true("apply: adjusted <= TA confidence for ALL multipliers", monotone_ok)

    # AI can NEVER create an entry: signal False stays False regardless of mult
    no_create = all(
        apply_ai_multiplier(0.0, m / 10.0, signal=False)[0] is False
        for m in range(0, 31)
    )
    check_true("apply: signal=False never flipped True by any multiplier", no_create)

    # --- parse_multiplier: extracts JSON, fails OPEN to neutral 1.0 ---
    m, _ = parse_multiplier('{"multiplier": 0.4, "rationale": "bearish"}')
    check("parse: valid JSON multiplier", m, 0.4)
    m, _ = parse_multiplier('noise before {"multiplier": 0.0} trailing text')
    check("parse: embedded JSON", m, 0.0)
    m, _ = parse_multiplier('{"multiplier": 9}')
    check("parse: clamps out-of-range to 1.0", m, 1.0)
    m, _ = parse_multiplier("not json at all")
    check("parse: no JSON -> neutral 1.0", m, 1.0)
    m, _ = parse_multiplier('{"rationale": "missing multiplier"}')
    check("parse: missing key -> neutral 1.0", m, 1.0)

    print(f"\n{_passed} passed, {_failed} failed.")
    if _failed == 0:
        print(
            "PHASE 6 LOGIC GREEN LIGHT (offline). Live AI run still required: "
            "wire an Anthropic key, set ai_enabled=true, and confirm advisory "
            "dampening + no autonomous watchlist changes."
        )
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
