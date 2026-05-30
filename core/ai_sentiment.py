"""ai_sentiment — news sentiment as a confidence multiplier (Phase 6, OFF by default).

Pulls recent news for a symbol from the Alpaca news API (no new dependency, no
new credentials — the same Alpaca account), asks Claude whether the news flow is
a bullish tailwind or a bearish headwind for a LONG entry, and returns a
multiplier in [0, 1].

  - bullish / neutral news -> ~1.0  (no dampening; TA stands)
  - bearish news           -> < 1.0 (dampen the TA confidence)
  - strongly bearish        -> ~0.0  (effective veto)

It can only ever SUBTRACT (invariant #2). **Fail-open:** if the news fetch or the
Anthropic call fails (or there is simply no news), it returns a NEUTRAL 1.0 so
the TA pipeline is unchanged — the AI layer is advisory and must never block a
trade by erroring.

The Alpaca + Anthropic SDKs are imported lazily by their respective client
factories, so importing this module is cheap and dependency-free.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from core import ai_client
from core.ai_client import AIParams

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a risk-aware trading research assistant. You judge whether recent "
    "news for a crypto asset is a tailwind or a headwind for opening a NEW LONG "
    "position right now. You output ONLY a confidence MULTIPLIER in [0,1] that "
    "will be applied to a technical-analysis confidence score:\n"
    "  1.0  = news is neutral or supportive of a long; do not dampen.\n"
    "  0.5  = mixed/uncertain news; dampen moderately.\n"
    "  0.0  = clearly bearish/risk-off news (hack, regulatory crackdown, "
    "delisting, depeg, major negative catalyst); effectively veto the long.\n"
    "You can ONLY lower or keep confidence — never raise it. When in doubt or "
    "when there is little/no relevant news, return 1.0 (neutral). "
    'Reply with ONLY compact JSON: {"multiplier": <0..1>, "rationale": "<short>"}.'
)


@dataclass(frozen=True)
class SentimentResult:
    multiplier: float          # [0,1]; 1.0 = neutral / no dampening
    rationale: str
    n_news: int


def neutral(reason: str = "sentiment disabled") -> SentimentResult:
    return SentimentResult(1.0, reason, 0)


def make_news_client(api_key: str, api_secret: str):
    """Alpaca historical news client (SDK imported lazily)."""
    from alpaca.data.historical.news import NewsClient

    return NewsClient(api_key, api_secret)


def _base_asset(symbol: str) -> str:
    """'BTC/USD' -> 'BTC'. Used as the news query symbol for crypto."""
    return symbol.split("/", 1)[0]


def fetch_news(news_client, symbol: str, params: AIParams) -> list[str]:
    """Return up to ``params.max_news`` recent headline/summary strings.

    Best-effort: tries the full symbol then the base asset (Alpaca news symbol
    formats vary). Any failure returns [] so the caller stays neutral.
    """
    from datetime import datetime, timezone

    from alpaca.data.requests import NewsRequest

    start = datetime.now(timezone.utc) - timedelta(hours=params.news_lookback_hours)
    for query in (symbol, _base_asset(symbol)):
        try:
            req = NewsRequest(
                symbols=query, start=start, limit=params.max_news, include_content=False
            )
            news_set = news_client.get_news(req)
            items = getattr(news_set, "data", {}).get("news", []) or getattr(
                news_set, "news", []
            )
            headlines: list[str] = []
            for item in items[: params.max_news]:
                headline = getattr(item, "headline", "") or ""
                summary = getattr(item, "summary", "") or ""
                text = headline if not summary else f"{headline} — {summary}"
                if text.strip():
                    headlines.append(text.strip())
            if headlines:
                return headlines
        except Exception:
            logger.exception("sentiment: news fetch failed for %s (query=%s)", symbol, query)
    return []


def assess_sentiment(
    news_client, anthropic_client, symbol: str, params: AIParams
) -> SentimentResult:
    """News-sentiment multiplier for one symbol. Fail-open to neutral 1.0."""
    headlines = fetch_news(news_client, symbol, params)
    if not headlines:
        return neutral("no recent news -> neutral")

    bullet_list = "\n".join(f"- {h}" for h in headlines)
    user = (
        f"Asset: {symbol}\n"
        f"Recent news ({len(headlines)} item(s), last {params.news_lookback_hours}h):\n"
        f"{bullet_list}\n\n"
        "Give the long-entry confidence multiplier as JSON."
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
        logger.exception("sentiment: anthropic call failed for %s", symbol)
        return SentimentResult(1.0, f"api error ({type(exc).__name__}) -> neutral", len(headlines))

    mult, rationale = ai_client.parse_multiplier(reply)
    return SentimentResult(mult, rationale, len(headlines))
