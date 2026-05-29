"""Phase 1 green-light test.

Runs four independent checks and prints a PASS/FAIL line for each:
  1. DB reachable                       (SELECT 1 against SQL Server)
  2. Decrypt Alpaca creds               (active paper row decrypts cleanly)
  3. Alpaca paper account               (returns equity/cash)
  4. Live crypto quote                  (returns bid/ask)

All four must PASS for Phase 1 green light. Each check runs even if an earlier
one fails (except those that genuinely need decrypted creds), so a single run
surfaces every problem at once. Exit code is 0 only if all four PASS.

Usage (Linux VPS):
    set -a; source .env; set +a
    python -m scripts.check_connectivity
"""
from __future__ import annotations

from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import text

from core.db import connect_with_retry, get_active_credentials

PROVIDER = "alpaca"
ENVIRONMENT = "paper"
DEFAULT_SYMBOL = "BTC/USD"


def _label(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _first_watchlist_symbol() -> Optional[str]:
    try:
        with connect_with_retry() as conn:
            row = conn.execute(
                text(
                    "SELECT TOP 1 symbol FROM dbo.watchlist "
                    "WHERE is_active = 1 ORDER BY id"
                )
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def check_db() -> tuple[bool, str]:
    with connect_with_retry() as conn:
        conn.execute(text("SELECT 1")).scalar()
    return True, "DB reachable"


def check_decrypt() -> tuple[bool, str, Optional[tuple[str, str]]]:
    api_key, api_secret = get_active_credentials(PROVIDER, ENVIRONMENT)
    masked = (api_key[:4] + "...") if len(api_key) > 4 else "set"
    return True, f"Decrypt Alpaca creds (key starts '{masked}')", (api_key, api_secret)


def check_account(creds: tuple[str, str]) -> tuple[bool, str]:
    from alpaca.trading.client import TradingClient

    api_key, api_secret = creds
    client = TradingClient(api_key, api_secret, paper=True)
    account = client.get_account()
    return True, f"Alpaca paper account (equity={account.equity}, cash={account.cash})"


def check_quote(creds: tuple[str, str]) -> tuple[bool, str]:
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoLatestQuoteRequest

    api_key, api_secret = creds
    symbol = _first_watchlist_symbol() or DEFAULT_SYMBOL
    client = CryptoHistoricalDataClient(api_key, api_secret)
    request = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
    quote = client.get_crypto_latest_quote(request)[symbol]
    return True, f"Live crypto quote {symbol} (bid={quote.bid_price}, ask={quote.ask_price})"


def _run(check, *args) -> tuple[bool, str]:
    try:
        result = check(*args)
    except Exception as exc:  # surface the failure, keep running the rest
        name = check.__name__.replace("check_", "").replace("_", " ")
        return False, f"{name} - {type(exc).__name__}: {exc}"
    # check_decrypt returns a 3-tuple (ok, msg, creds); normalize to (ok, msg)
    return result[0], result[1]


def main() -> int:
    load_dotenv()

    results: list[tuple[bool, str]] = []
    creds: Optional[tuple[str, str]] = None

    # 1. DB reachable
    results.append(_run(check_db))

    # 2. Decrypt creds (capture creds for checks 3 & 4)
    try:
        ok, msg, creds = check_decrypt()
    except Exception as exc:
        ok, msg = False, f"decrypt - {type(exc).__name__}: {exc}"
    results.append((ok, msg))

    # 3. Paper account
    if creds:
        results.append(_run(check_account, creds))
    else:
        results.append((False, "account — skipped (no decrypted creds)"))

    # 4. Live quote
    if creds:
        results.append(_run(check_quote, creds))
    else:
        results.append((False, "quote — skipped (no decrypted creds)"))

    print()
    for ok, msg in results:
        print(f"[{_label(ok)}] {msg}")
    print()

    all_ok = all(ok for ok, _ in results)
    print(
        "PHASE 1 GREEN LIGHT - proceed to Phase 2."
        if all_ok
        else "Phase 1 NOT green - fix the FAIL line(s) above and re-run."
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
