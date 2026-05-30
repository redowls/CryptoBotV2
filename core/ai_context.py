"""ai_context — price-context reasoning as a confidence multiplier (Phase 6, OFF by default).

Feeds the TA engine's indicator snapshot + a compact recent-price summary to
Claude and asks whether the long setup looks like a trap (e.g. badly
overextended, buying straight into resistance, a low-quality late-cycle chase)
versus a clean continuation. Returns a multiplier in [0, 1].

  - clean / healthy setup  -> ~1.0 (no dampening)
  - questionable setup     -> < 1.0 (dampen)
  - clearly poor / trap     -> ~0.0 (effective veto)

Like every Phase 6 module it can only SUBTRACT (invariant #2) and **fails open**
to a neutral 1.0 on any error — the TA decision stands if the AI cannot help.
This module makes NO market-data calls of its own; it reasons over the snapshot
the TA engine already produced (no look-ahead, no extra API cost).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from core import ai_client
from core.ai_client import AIParams

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a risk-aware technical analyst. Given indicator readings and a "
    "short recent-price summary for a crypto asset, judge the QUALITY of opening "
    "a NEW LONG now. Output ONLY a confidence MULTIPLIER in [0,1] applied to a "
    "technical-analysis confidence score:\n"
    "  1.0 = clean, healthy long setup; do not dampen.\n"
    "  0.5 = questionable (e.g. extended, into resistance, weak structure).\n"
    "  0.0 = clearly poor / likely trap; effectively veto.\n"
    "You can ONLY lower or keep confidence — never raise it. The TA engine has "
    "already approved the BUY gate; your job is to catch obvious traps, not to "
    "re-derive the signal. When unsure, return 1.0 (neutral). "
    'Reply with ONLY compact JSON: {"multiplier": <0..1>, "rationale": "<short>"}.'
)


@dataclass(frozen=True)
class ContextResult:
    multiplier: float          # [0,1]; 1.0 = neutral / no dampening
    rationale: str


def neutral(reason: str = "context disabled") -> ContextResult:
    return ContextResult(1.0, reason)


def _price_summary(closes: Sequence[float]) -> str:
    """Compact recent-price line: last close, and % change over ~6/24 bars."""
    if not closes:
        return "no recent closes"
    last = closes[-1]

    def pct(n: int) -> Optional[float]:
        if len(closes) > n and closes[-1 - n]:
            return (last / closes[-1 - n] - 1.0) * 100.0
        return None

    chg6, chg24 = pct(6), pct(24)
    parts = [f"last close {last:,.6g}"]
    if chg6 is not None:
        parts.append(f"6-bar change {chg6:+.2f}%")
    if chg24 is not None:
        parts.append(f"24-bar change {chg24:+.2f}%")
    return ", ".join(parts)


def assess_context(
    anthropic_client,
    symbol: str,
    *,
    snapshot: dict,
    closes: Sequence[float],
    confidence: float,
    params: AIParams,
) -> ContextResult:
    """Price-context multiplier for one symbol. Fail-open to neutral 1.0."""
    components = snapshot.get("components", {}) if isinstance(snapshot, dict) else {}
    indicators = {
        k: snapshot.get(k)
        for k in ("ema_fast", "ema_slow", "rsi", "atr", "volume_z", "support", "resistance")
        if isinstance(snapshot, dict)
    }
    user = (
        f"Asset: {symbol}\n"
        f"TA confidence (pre-AI): {confidence:.0f}/100\n"
        f"Price: {_price_summary(closes)}\n"
        f"Indicators: {indicators}\n"
        f"TA components: {components}\n\n"
        "Give the long-entry quality multiplier as JSON."
    )
    try:
        reply = ai_client.request_text(
            anthropic_client,
            model=params.model,
            system=_SYSTEM,
            user=user,
            max_tokens=params.max_tokens,
        )
    except Exception as exc:
        logger.exception("context: anthropic call failed for %s", symbol)
        return ContextResult(1.0, f"api error ({type(exc).__name__}) -> neutral")

    mult, rationale = ai_client.parse_multiplier(reply)
    return ContextResult(mult, rationale)
