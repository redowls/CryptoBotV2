"""Fernet encryption/decryption for stored API credentials.

The master key (``TRADEBOT_MASTER_KEY``) lives only in the process environment,
loaded from a chmod-600 ``.env`` owned by the dedicated non-root bot user. The
database stores only Fernet ciphertext; plaintext credentials are never
persisted, logged, or echoed. Losing the master key makes every stored
credential undecryptable — back it up offline.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

MASTER_KEY_ENV = "TRADEBOT_MASTER_KEY"

_GENERATE_HINT = (
    "Generate one with: "
    "python -c \"from cryptography.fernet import Fernet; "
    "print(Fernet.generate_key().decode())\""
)


def _fernet() -> Fernet:
    key = os.environ.get(MASTER_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"{MASTER_KEY_ENV} is not set. Load it from your chmod-600 .env "
            f"before running anything that touches credentials."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{MASTER_KEY_ENV} is not a valid Fernet key. {_GENERATE_HINT}"
        ) from exc


def encrypt(plaintext: str) -> bytes:
    """Encrypt a plaintext secret into Fernet token bytes for VARBINARY storage."""
    if not isinstance(plaintext, str):
        raise TypeError("plaintext must be a str")
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(token: "bytes | bytearray | memoryview | str") -> str:
    """Decrypt a Fernet token (bytes from the DB, or str) back to plaintext.

    Raises RuntimeError if the current master key does not match the key used
    to encrypt the value (e.g. the key was rotated or lost).
    """
    if isinstance(token, str):
        token = token.encode("utf-8")
    try:
        return _fernet().decrypt(bytes(token)).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            f"Failed to decrypt credential: {MASTER_KEY_ENV} does not match the "
            f"key used to encrypt this value."
        ) from exc
