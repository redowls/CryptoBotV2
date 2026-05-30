"""ai_advisor — combine the AI modules into ONE confidence multiplier (Phase 6).

This is the single entry point the orchestrator calls. It:

  1. short-circuits to NEUTRAL (multiplier 1.0) when ``ai_enabled`` is false or
     no Anthropic key is configured — so the TA pipeline is byte-for-byte
     unchanged whenever the AI layer is off or unprovisioned;
  2. runs the enabled modules (sentiment, context), each of which fails OPEN to
     1.0 on any error;
  3. combines them by PRODUCT, bounded below by ``ai.min_multiplier`` (the cap
     on how far the AI may dampen) — the pure :func:`ai_client.combine_multipliers`;
  4. applies the multiplier to TA confidence via :func:`ai_client.apply_ai_multiplier`
     (which leaves the TA ``signal`` bool UNTOUCHED — invariant #2); and
  5. logs the whole decision to ``dbo.ai_suggestions`` (audit only — it never
     gates a trade itself; the orchestrator does the gating).

Invariant #2 is enforced structurally: the returned ``multiplier`` is always in
[0,1], the ``signal`` is never changed, and a disabled/failed AI is neutral.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from core import ai_client, ai_context, ai_sentiment, config, db
from core.ai_client import AIParams

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AIAdvice:
    symbol: str
    enabled: bool                 # AI actually ran (vs neutral short-circuit)
    ta_confidence: float
    multiplier: float             # combined, in [0,1]
    adjusted_confidence: float    # ta_confidence * multiplier
    signal: bool                  # passed THROUGH from TA, never changed by AI
    sentiment_multiplier: Optional[float]
    context_multiplier: Optional[float]
    rationale: str

    @property
    def dampened(self) -> bool:
        return self.multiplier < 1.0

    def summary(self) -> str:
        bits = []
        if self.sentiment_multiplier is not None:
            bits.append(f"sent={self.sentiment_multiplier:.2f}")
        if self.context_multiplier is not None:
            bits.append(f"ctx={self.context_multiplier:.2f}")
        return " ".join(bits) if bits else "neutral"


def _neutral(symbol: str, *, ta_confidence: float, signal: bool, reason: str) -> AIAdvice:
    """A no-op advice: multiplier 1.0, confidence unchanged, signal passed through."""
    return AIAdvice(
        symbol=symbol,
        enabled=False,
        ta_confidence=ta_confidence,
        multiplier=1.0,
        adjusted_confidence=ta_confidence,
        signal=signal,
        sentiment_multiplier=None,
        context_multiplier=None,
        rationale=reason,
    )


def _persist(advice: AIAdvice, *, bar_ts, model: str, floor_vetoed: bool) -> None:
    """Append the advisory row to dbo.ai_suggestions (audit only)."""
    try:
        with db.connect_with_retry() as conn:
            with conn.begin():
                conn.execute(
                    db.text(
                        "INSERT INTO dbo.ai_suggestions "
                        "  (symbol, bar_ts_utc, ta_confidence, sentiment_multiplier, "
                        "   context_multiplier, combined_multiplier, adjusted_confidence, "
                        "   vetoed, model, rationale) "
                        "VALUES (:symbol, :bar_ts, :ta, :sent, :ctx, :comb, :adj, "
                        "        :vetoed, :model, :rationale)"
                    ),
                    {
                        "symbol": advice.symbol,
                        "bar_ts": bar_ts,
                        "ta": round(advice.ta_confidence, 2),
                        "sent": advice.sentiment_multiplier,
                        "ctx": advice.context_multiplier,
                        "comb": round(advice.multiplier, 4),
                        "adj": round(advice.adjusted_confidence, 2),
                        "vetoed": 1 if floor_vetoed else 0,
                        "model": model,
                        "rationale": advice.rationale[:3900],
                    },
                )
    except Exception:
        # Logging the advice must never break the trade cycle.
        logger.exception("ai_advisor: failed to persist ai_suggestions for %s", advice.symbol)


def advise(
    symbol: str,
    *,
    ta_confidence: float,
    signal: bool,
    snapshot: dict,
    closes: Sequence[float],
    bar_ts=None,
    floor: Optional[float] = None,
    dry_run: bool = False,
    params: Optional[AIParams] = None,
) -> AIAdvice:
    """Produce the combined AI multiplier for a TA signal. Always fail-open.

    ``floor`` (the trade confidence floor) is used ONLY to flag a veto in the
    audit log; the orchestrator owns the actual entry decision. When ``dry_run``
    is true the advice is computed and returned but NOT persisted.
    """
    params = params or AIParams.from_config()

    if not params.enabled:
        return _neutral(symbol, ta_confidence=ta_confidence, signal=signal,
                        reason="ai_enabled=false -> neutral")

    key = ai_client.anthropic_key(params)
    if not key:
        logger.warning("ai_advisor: ai_enabled but no Anthropic key configured -> neutral")
        return _neutral(symbol, ta_confidence=ta_confidence, signal=signal,
                        reason="no Anthropic key -> neutral")

    try:
        client = ai_client.make_client(key, timeout=params.timeout_secs)
    except Exception as exc:
        logger.exception("ai_advisor: client build failed -> neutral")
        return _neutral(symbol, ta_confidence=ta_confidence, signal=signal,
                        reason=f"client error ({type(exc).__name__}) -> neutral")

    sent_mult: Optional[float] = None
    ctx_mult: Optional[float] = None
    rationales: list[str] = []

    if params.sentiment_enabled:
        try:
            environment = config.get_str("environment", "paper")
            ak, asec = db.get_active_credentials("alpaca", environment)
            news_client = ai_sentiment.make_news_client(ak, asec)
            s = ai_sentiment.assess_sentiment(news_client, client, symbol, params)
            sent_mult = s.multiplier
            rationales.append(f"sentiment[{s.n_news}n]: {s.rationale}")
        except Exception:
            logger.exception("ai_advisor: sentiment failed for %s -> neutral", symbol)
            sent_mult = 1.0
            rationales.append("sentiment: error -> neutral")

    if params.context_enabled:
        c = ai_context.assess_context(
            client, symbol, snapshot=snapshot, closes=closes,
            confidence=ta_confidence, params=params,
        )
        ctx_mult = c.multiplier
        rationales.append(f"context: {c.rationale}")

    components = [m for m in (sent_mult, ctx_mult) if m is not None]
    multiplier = (
        ai_client.combine_multipliers(components, floor=params.min_multiplier)
        if components else 1.0
    )
    _sig, adjusted = ai_client.apply_ai_multiplier(ta_confidence, multiplier, signal=signal)

    advice = AIAdvice(
        symbol=symbol,
        enabled=True,
        ta_confidence=ta_confidence,
        multiplier=multiplier,
        adjusted_confidence=adjusted,
        signal=_sig,
        sentiment_multiplier=sent_mult,
        context_multiplier=ctx_mult,
        rationale=" | ".join(rationales) if rationales else "no modules enabled",
    )

    floor_vetoed = floor is not None and adjusted < floor
    if not dry_run:
        _persist(advice, bar_ts=bar_ts, model=params.model, floor_vetoed=floor_vetoed)
    return advice
