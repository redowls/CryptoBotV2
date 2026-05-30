r"""Phase 4 manual Position Manager run — manage open positions ONCE.

For every open position (optionally filtered to given symbols), fetch the latest
closed bar and: check the two stop triggers + TP (first-to-fire wins, invariant
#5), and if no exit, trail the SL/support up and expand the TP for the next bar.
This is the EXIT half of the hourly cycle, runnable on its own at a different
cadence than the entry scan.

Flags:
    --dry-run   Report intended exits/adjustments but place NO orders and write
                NOTHING to the DB. Hits the paper account + market data only.

Usage:
    .\.venv\Scripts\python.exe -m scripts.run_position_manager                # all open positions
    .\.venv\Scripts\python.exe -m scripts.run_position_manager --dry-run      # report only
    .\.venv\Scripts\python.exe -m scripts.run_position_manager BTC/USD        # one symbol
"""
from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from core.position_manager import manage_open_positions


def main(argv: list[str]) -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    dry_run = "--dry-run" in argv
    symbols = [a for a in argv if not a.startswith("--")] or None

    results = manage_open_positions(symbols=symbols, dry_run=dry_run)

    exits = [r for r in results if r.reason]
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"\nPosition Manager ({mode}): {len(exits)} exit(s) of {len(results)} acted.")
    for r in exits:
        px = f"@{r.exit_price:,.2f}" if r.exit_price else "(simulated)"
        print(f"  pos#{r.position_id} {r.symbol} -> {r.reason} {px}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
