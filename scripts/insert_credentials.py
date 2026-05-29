"""Encrypt and store API credentials in SQL Server.

YOU run this locally / on the VPS. Claude never sees raw keys. The secret is
read via getpass (not echoed), encrypted with the master key, and only the
Fernet ciphertext is written to the database. Inserting a new active row for a
provider/environment deactivates the previous active row (rotation).

Usage (Linux VPS):
    set -a; source .env; set +a
    python -m scripts.insert_credentials
"""
from __future__ import annotations

import getpass
import sys

from dotenv import load_dotenv
from sqlalchemy import text

from core import crypto
from core.db import connect_with_retry


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def main() -> int:
    load_dotenv()
    print("Store encrypted API credentials.\n")

    provider = _prompt("Provider", "alpaca")
    environment = _prompt("Environment", "paper")
    key_label = _prompt("Key label (optional)", f"{provider}-{environment}")

    api_key = getpass.getpass("API key (input hidden): ").strip()
    api_secret = getpass.getpass("API secret (input hidden): ").strip()
    if not api_key or not api_secret:
        print("ERROR: API key and secret are both required.", file=sys.stderr)
        return 1

    enc_key = crypto.encrypt(api_key)
    enc_secret = crypto.encrypt(api_secret)
    # Drop plaintext from locals as soon as it is encrypted.
    api_key = api_secret = ""

    with connect_with_retry() as conn:
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE dbo.api_credentials "
                    "SET is_active = 0, rotated_at_utc = SYSUTCDATETIME() "
                    "WHERE provider = :provider AND environment = :environment "
                    "  AND is_active = 1"
                ),
                {"provider": provider, "environment": environment},
            )
            conn.execute(
                text(
                    "INSERT INTO dbo.api_credentials "
                    "(provider, key_label, environment, encrypted_api_key, "
                    " encrypted_api_secret, is_active) "
                    "VALUES (:provider, :label, :environment, :enc_key, "
                    " :enc_secret, 1)"
                ),
                {
                    "provider": provider,
                    "label": key_label,
                    "environment": environment,
                    "enc_key": enc_key,
                    "enc_secret": enc_secret,
                },
            )

    print(
        f"\nStored active {provider}/{environment} credentials "
        f"(label: {key_label}). Plaintext was never persisted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
