r"""Manually flatten a single OPEN position on paper (exit_reason='manual').

Liquidates the whole wallet position via the broker, then records the exit to
`trade_history` (realized P&L from the ACTUAL fill) and flips the position to
closed. Use this to clear a synthetic test position, or for any manual flatten.

The exit goes through the same Phase 4 path as an automatic stop
(`execution.close_position` + `execution.record_exit`); only the exit_reason
differs ('manual' vs price_sl/support_break/tp).

Usage:
    .\.venv\Scripts\python.exe -m scripts.flatten_position 1            # flatten position id 1
    .\.venv\Scripts\python.exe -m scripts.flatten_position 1 --dry-run  # report only, NO order / NO DB write
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from core import config, db
from core.execution import close_position, make_trading_client, record_exit


def main(argv: list[str]) -> int:
    load_dotenv()
    dry_run = "--dry-run" in argv
    ids = [a for a in argv if not a.startswith("--")]
    if len(ids) != 1 or not ids[0].isdigit():
        print("usage: python -m scripts.flatten_position <position_id> [--dry-run]",
              file=sys.stderr)
        return 2
    position_id = int(ids[0])

    with db.connect_with_retry() as conn:
        row = conn.execute(
            db.text(
                "SELECT id, symbol, qty, entry_price, entry_ts_utc, status "
                "FROM dbo.positions WHERE id = :pid"
            ),
            {"pid": position_id},
        ).fetchone()

    if row is None:
        print(f"position #{position_id} not found")
        return 1
    if row.status != "open":
        print(f"position #{position_id} is already {row.status!r} - nothing to do")
        return 1

    symbol = row.symbol
    entry_price = float(row.entry_price)
    print(f"Flatten pos#{position_id} {symbol}: qty~{float(row.qty)} entry={entry_price:,.2f}")

    if dry_run:
        print("DRY-RUN: would liquidate the wallet position and write a "
              "trade_history row (exit_reason='manual'). No order placed.")
        return 0

    key, secret = db.get_active_credentials(
        "alpaca", config.get_str("environment", "paper")
    )
    client = make_trading_client(
        key, secret, paper=(config.get_str("environment", "paper") != "live")
    )

    fill = close_position(
        client,
        symbol,
        poll_attempts=config.get_int("execution.poll_attempts", 5),
        poll_delay=config.get_float("execution.poll_delay_secs", 1.0),
    )
    print(f"  liquidation: status={fill.status} filled_qty={fill.filled_qty} "
          f"avg_price={fill.avg_price}")
    if not fill.is_filled:
        print("  order did not fill - leaving the position OPEN, no DB write")
        return 1

    with db.connect_with_retry() as conn:
        with conn.begin():
            record_exit(
                conn,
                position_id=position_id,
                symbol=symbol,
                qty=fill.filled_qty,
                entry_price=entry_price,
                exit_price=fill.avg_price,
                opened_at_utc=row.entry_ts_utc,
                exit_reason="manual",
            )
    realized = (fill.avg_price - entry_price) * fill.filled_qty
    print(f"  CLOSED pos#{position_id}: exit={fill.avg_price:,.2f} "
          f"realized_pnl={realized:,.4f} (exit_reason='manual')")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
