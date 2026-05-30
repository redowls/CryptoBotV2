r"""Phase 3 manual trade cycle — runs ONE hourly orchestrator cycle.

For each active watchlist symbol: refresh closed bars, recompute + persist S/R,
run the TA engine, and on a BUY setup (not already held, confidence >= floor,
not in cool-down) size the trade and place a paper order, recording the actual
fill plus initial TP, price-SL, and support-break trigger.

Flags:
    --dry-run   Size and report intended entries but place NO orders. Hits the
                paper account + market data only; writes bars/levels/signals.
    --force     DEV ONLY. Synthesize a BUY signal for the listed symbols so an
                entry can be exercised end-to-end (the Phase 3 green-light
                test). Use WITH explicit symbols, e.g. --force BTC/USD.

Usage:
    .\.venv\Scripts\python.exe -m scripts.run_trade_cycle                 # all active, real signals, places orders
    .\.venv\Scripts\python.exe -m scripts.run_trade_cycle --dry-run       # size only, no orders
    .\.venv\Scripts\python.exe -m scripts.run_trade_cycle BTC/USD         # one symbol
    .\.venv\Scripts\python.exe -m scripts.run_trade_cycle --force BTC/USD # DEV: force an entry on paper
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from core.orchestrator import run_cycle


def main(argv: list[str]) -> int:
    load_dotenv()
    dry_run = "--dry-run" in argv
    force = "--force" in argv
    symbols = [a for a in argv if not a.startswith("--")]

    if force and not symbols:
        print(
            "--force requires explicit symbol(s), e.g. --force BTC/USD",
            file=sys.stderr,
        )
        return 2

    return run_cycle(symbols=symbols, dry_run=dry_run, force=force)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
