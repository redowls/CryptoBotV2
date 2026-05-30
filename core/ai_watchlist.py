"""ai_watchlist — suggest watchlist add/remove, for REVIEW ONLY (Phase 6, OFF by default).

Asks Claude, given the CURRENT active watchlist, to propose symbols worth adding
or removing. Proposals are written to ``dbo.ai_watchlist_suggestions`` with
``status='pending'`` and are **NEVER auto-applied** to ``dbo.watchlist`` — a
human reviews and acts. This module does not touch the watchlist table at all;
that separation is the Phase 6 green-light guarantee ("no autonomous changes to
the watchlist").

This is an ADMIN-run helper (``scripts/run_ai_review``), not part of the hourly
trade cycle, so the cost lands only when you ask for it. Fail-open: any error
yields an empty suggestion list and changes nothing.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from core import ai_client, config, db
from core.ai_client import AIParams

logger = logging.getLogger(__name__)

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.S)

_SYSTEM = (
    "You are a crypto trading research assistant maintaining a tradeable "
    "watchlist for an automated LONG-only, hourly, technical-analysis bot on "
    "Alpaca crypto pairs. Given the current active watchlist, propose a SMALL "
    "number of high-conviction changes: liquid major pairs worth ADDING, and "
    "currently-listed symbols worth REMOVING (illiquid, deprecated, or "
    "structurally weak). Be conservative — suggest nothing rather than noise. "
    "Use Alpaca pair format like 'BTC/USD'. Reply with ONLY a compact JSON "
    'array: [{"action":"add|remove","symbol":"X/USD","rationale":"<short>"}]. '
    "Return [] if no change is warranted."
)


@dataclass(frozen=True)
class WatchlistSuggestion:
    action: str        # 'add' | 'remove'
    symbol: str
    rationale: str


def _active_watchlist() -> list[str]:
    with db.connect_with_retry() as conn:
        rows = conn.execute(
            db.text("SELECT symbol FROM dbo.watchlist WHERE is_active = 1 ORDER BY id")
        ).fetchall()
    return [r[0] for r in rows]


def _parse(reply: str) -> list[WatchlistSuggestion]:
    try:
        match = _JSON_ARRAY_RE.search(reply or "")
        if not match:
            return []
        items = json.loads(match.group(0))
    except Exception:
        logger.exception("ai_watchlist: failed to parse reply")
        return []

    out: list[WatchlistSuggestion] = []
    for item in items if isinstance(items, list) else []:
        action = str(item.get("action", "")).strip().lower()
        symbol = str(item.get("symbol", "")).strip().upper()
        rationale = str(item.get("rationale", "")).strip()[:1000]
        if action in ("add", "remove") and symbol:
            out.append(WatchlistSuggestion(action, symbol, rationale))
    return out


def _persist(suggestions: list[WatchlistSuggestion], model: str) -> None:
    with db.connect_with_retry() as conn:
        with conn.begin():
            for s in suggestions:
                conn.execute(
                    db.text(
                        "INSERT INTO dbo.ai_watchlist_suggestions "
                        "  (symbol, action, rationale, model, status) "
                        "VALUES (:symbol, :action, :rationale, :model, 'pending')"
                    ),
                    {
                        "symbol": s.symbol,
                        "action": s.action,
                        "rationale": s.rationale,
                        "model": model,
                    },
                )


def suggest_watchlist_changes(
    *, dry_run: bool = False, params: Optional[AIParams] = None
) -> list[WatchlistSuggestion]:
    """Return AI watchlist suggestions (written to the review table unless dry_run).

    NEVER modifies dbo.watchlist. Fail-open: returns [] and writes nothing on
    any error or when the AI layer / key is unavailable.
    """
    params = params or AIParams.from_config()
    if not params.enabled or not params.watchlist_enabled:
        logger.info("ai_watchlist: disabled (ai_enabled/ai.watchlist_enabled) -> no suggestions")
        return []

    key = ai_client.anthropic_key(params)
    if not key:
        logger.warning("ai_watchlist: no Anthropic key configured -> no suggestions")
        return []

    current = _active_watchlist()
    user = (
        f"Current active watchlist ({len(current)}): {', '.join(current) or '(empty)'}\n\n"
        "Propose conservative add/remove changes as a JSON array."
    )
    try:
        client = ai_client.make_client(key, timeout=params.timeout_secs)
        reply = ai_client.request_text(
            client, model=params.model, system=_SYSTEM, user=user,
            max_tokens=params.max_tokens,
        )
    except Exception:
        logger.exception("ai_watchlist: anthropic call failed -> no suggestions")
        return []

    suggestions = _parse(reply)
    if suggestions and not dry_run:
        _persist(suggestions, params.model)
    return suggestions
