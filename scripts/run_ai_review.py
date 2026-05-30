r"""Phase 6 admin tool — review the AI research layer's output (read-only-ish).

For each symbol it runs the TA engine, then the AI advisor (sentiment + context)
and prints the confidence multiplier the AI WOULD apply — so you can see how the
advisory layer behaves before/while ``ai_enabled`` drives the live cycle. With
``--watchlist`` it also asks the AI for watchlist add/remove suggestions.

By default this is a DRY-RUN preview: it computes advice but writes NOTHING.
Pass ``--write`` to persist the advisory rows (ai_suggestions /
ai_watchlist_suggestions). It NEVER places orders and NEVER modifies the
watchlist (watchlist suggestions only ever land in the review table).

Note: the AI only runs when ``ai_enabled=true`` AND an Anthropic key is
configured (ANTHROPIC_API_KEY env var, or an encrypted api_credentials row with
provider='anthropic'). Otherwise every symbol shows a NEUTRAL x1.00 — which is
exactly the safe default: no key, no AI influence.

Usage:
    .\.venv\Scripts\python.exe -m scripts.run_ai_review                # all active symbols (dry-run)
    .\.venv\Scripts\python.exe -m scripts.run_ai_review BTC/USD        # specific symbol(s)
    .\.venv\Scripts\python.exe -m scripts.run_ai_review --watchlist    # also AI watchlist suggestions
    .\.venv\Scripts\python.exe -m scripts.run_ai_review --write        # persist advisory rows
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from core import ai_advisor, ai_watchlist, config, db
from core.ai_client import AIParams
from core.market_data import fetch_closed_bars, make_client
from core.sizing import load_active_risk_profile
from core.ta_engine import TAParams, evaluate


def _active_symbols() -> list[str]:
    with db.connect_with_retry() as conn:
        rows = conn.execute(
            db.text("SELECT symbol FROM dbo.watchlist WHERE is_active = 1 ORDER BY id")
        ).fetchall()
    return [r[0] for r in rows]


def main(argv: list[str]) -> int:
    load_dotenv()
    do_watchlist = "--watchlist" in argv
    write = "--write" in argv
    dry_run = not write
    explicit = [a for a in argv if not a.startswith("--")]

    params = AIParams.from_config()
    ta_params = TAParams.from_config()
    environment = config.get_str("environment", "paper")
    tf = config.get_str("scan.timeframe", "1Hour")
    limit = config.get_int("scan.bars_to_fetch", 250)

    print(
        f"\nAI review ({'WRITE' if write else 'dry-run, no writes'}) | "
        f"ai_enabled={params.enabled} model={params.model} "
        f"sentiment={params.sentiment_enabled} context={params.context_enabled} "
        f"min_mult={params.min_multiplier}"
    )
    if not params.enabled:
        print("  NOTE: ai_enabled=false -> advisor returns NEUTRAL x1.00 for every symbol.\n")
    else:
        from core.ai_client import anthropic_key

        if not anthropic_key(params):
            print("  NOTE: no Anthropic key configured -> NEUTRAL x1.00 (offline-only build).\n")
        else:
            print()

    key, secret = db.get_active_credentials("alpaca", environment)
    data_client = make_client(key, secret)

    with db.connect_with_retry() as conn:
        profile = load_active_risk_profile(conn)

    symbols = explicit if explicit else _active_symbols()
    if not symbols:
        print("No symbols to review.")
        return 1

    for symbol in symbols:
        try:
            bars = fetch_closed_bars(data_client, symbol, timeframe=tf, limit=limit)
        except Exception as exc:
            print(f"{symbol:<12} ERROR fetching bars: {type(exc).__name__}: {exc}")
            continue
        if not bars:
            print(f"{symbol:<12} no closed bars")
            continue

        sig = evaluate(symbol, bars, ta_params)
        advice = ai_advisor.advise(
            symbol,
            ta_confidence=sig.confidence,
            signal=sig.signal,
            snapshot=sig.snapshot,
            closes=[b.close for b in bars],
            bar_ts=sig.bar_ts,
            floor=profile.min_confidence_to_trade,
            dry_run=dry_run,
            params=params,
        )
        gate = "BUY" if sig.signal else "----"
        print(
            f"{symbol:<12} [{gate}] TA conf {sig.confidence:.0f} -> "
            f"x{advice.multiplier:.2f} -> {advice.adjusted_confidence:.0f}  "
            f"({advice.summary()})"
        )
        if advice.rationale:
            print(f"             {advice.rationale}")

    if do_watchlist:
        print("\nAI watchlist suggestions:")
        suggestions = ai_watchlist.suggest_watchlist_changes(dry_run=dry_run, params=params)
        if not suggestions:
            print("  (none)")
        for s in suggestions:
            print(f"  {s.action.upper():<6} {s.symbol:<12} {s.rationale}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
