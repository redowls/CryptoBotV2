"""AI research layer — shared params, Anthropic client, and the PURE
confidence-multiplier core (Phase 6, default OFF).

The whole point of this layer is **invariant #2: AI can only SUBTRACT.** Its
output is a multiplier in [0, 1] applied to the TA confidence. It can *veto*
(multiplier -> 0) or *dampen* (0 < m < 1) but it can NEVER:

  - raise confidence (the multiplier is clamped to <= 1), or
  - create an entry (the TA ``signal`` bool is passed through untouched).

A disabled OR erroring AI yields multiplier ``1.0`` — i.e. pure TA, unchanged.
AI failure must never alter the TA pipeline; that is why every live path in the
sibling modules fails *open* to a neutral 1.0.

The pure helpers (:func:`clamp01` / :func:`combine_multipliers` /
:func:`apply_ai_multiplier` / :func:`parse_multiplier`) take plain values, are
side-effect-free, and are the offline green-light target
(``scripts/verify_phase6``) — exactly like the Phase 4/5 decision helpers. The
Anthropic SDK is imported LAZILY inside :func:`make_client` so this module (and
the offline test) import cleanly without the ``anthropic`` package installed.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from core import config

# Matches the first {...} block in a model reply, across newlines.
_JSON_RE = re.compile(r"\{.*\}", re.S)


# ----------------------------------------------------------------------------
# Params
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class AIParams:
    enabled: bool                # master switch (app_config 'ai_enabled')
    model: str
    sentiment_enabled: bool
    context_enabled: bool
    watchlist_enabled: bool
    min_multiplier: float        # floor on how far AI may dampen (0 = full veto)
    max_news: int
    news_lookback_hours: int
    max_tokens: int
    timeout_secs: float
    key_environment: str         # api_credentials environment for provider='anthropic'

    @staticmethod
    def from_config() -> "AIParams":
        return AIParams(
            enabled=config.get_bool("ai_enabled", False),
            model=config.get_str("ai.model", "claude-opus-4-8"),
            sentiment_enabled=config.get_bool("ai.sentiment_enabled", True),
            context_enabled=config.get_bool("ai.context_enabled", True),
            watchlist_enabled=config.get_bool("ai.watchlist_enabled", False),
            min_multiplier=config.get_float("ai.min_multiplier", 0.0),
            max_news=config.get_int("ai.max_news", 10),
            news_lookback_hours=config.get_int("ai.news_lookback_hours", 24),
            max_tokens=config.get_int("ai.max_tokens", 1024),
            timeout_secs=config.get_float("ai.timeout_secs", 30.0),
            key_environment=config.get_str("ai.key_environment", "live"),
        )


# ----------------------------------------------------------------------------
# PURE multiplier core (offline-tested — invariant #2)
# ----------------------------------------------------------------------------
def clamp01(x: float) -> float:
    """Clamp to [0, 1]. AI multipliers can never exceed 1 (no raising)."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def combine_multipliers(multipliers: Sequence[float], *, floor: float = 0.0) -> float:
    """Combine component multipliers by PRODUCT (each can only subtract more).

    Every input is clamped to [0, 1] first, so the product lands in [0, 1]; the
    result is then bounded *below* by ``floor`` (the configured cap on how much
    the AI may dampen). ``floor`` itself is clamped to [0, 1]. The result is
    always in [0, 1], so the AI can never raise TA confidence.
    """
    m = 1.0
    for x in multipliers:
        m *= clamp01(x)
    return max(clamp01(floor), m)


def apply_ai_multiplier(
    ta_confidence: float, multiplier: float, *, signal: bool
) -> tuple[bool, float]:
    """Apply the AI multiplier to TA confidence. Returns ``(signal, adjusted)``.

    The ``signal`` bool is returned UNCHANGED — the AI layer cannot create an
    entry the TA engine did not approve (invariant #2). The multiplier is
    clamped to [0, 1] so ``adjusted <= ta_confidence`` ALWAYS: AI subtracts,
    never adds.
    """
    return signal, ta_confidence * clamp01(multiplier)


def parse_multiplier(text: str) -> tuple[float, str]:
    """Parse a ``{"multiplier": x, "rationale": "..."}`` reply.

    Returns ``(multiplier in [0,1], rationale)``. On ANY failure (no JSON, bad
    number, missing key) returns ``(1.0, reason)`` — NEUTRAL, so a malformed AI
    reply never alters the TA pipeline (fail-open, invariant #2 by omission).
    """
    try:
        match = _JSON_RE.search(text or "")
        if not match:
            return 1.0, "no JSON in reply -> neutral"
        data = json.loads(match.group(0))
        mult = clamp01(float(data["multiplier"]))
        rationale = str(data.get("rationale", ""))[:1000]
        return mult, rationale
    except Exception as exc:  # parse/format failure -> neutral, never raise
        return 1.0, f"parse failure ({type(exc).__name__}) -> neutral"


# ----------------------------------------------------------------------------
# Live Anthropic client (SDK imported lazily — offline-import-clean)
# ----------------------------------------------------------------------------
def anthropic_key(params: Optional[AIParams] = None) -> Optional[str]:
    """Resolve the Anthropic API key, or None if not configured.

    Precedence: ``ANTHROPIC_API_KEY`` env var first (the offline-friendly path),
    then an encrypted ``api_credentials`` row with provider='anthropic'. Returns
    None (not an error) when neither is present, so callers fail open to neutral.
    """
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return env_key
    params = params or AIParams.from_config()
    try:
        from core import db

        key, _secret = db.get_active_credentials("anthropic", params.key_environment)
        return key or None
    except Exception:
        return None


def make_client(api_key: str, *, timeout: float = 30.0):
    """Build an Anthropic client. The SDK import is lazy on purpose."""
    from anthropic import Anthropic

    return Anthropic(api_key=api_key, timeout=timeout)


def request_text(client, *, model: str, system: str, user: str, max_tokens: int) -> str:
    """One advisory messages.create call; returns concatenated text output.

    The system prompt is sent as a cached block (prompt caching) so repeated
    hourly calls with the same instructions reuse the cache. Raises on API
    error — callers wrap this and fail open to a neutral multiplier.
    """
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )
