"""SQL Server connectivity via SQLAlchemy + pyodbc (ODBC Driver 18).

Connection settings come from the environment (loaded from ``.env``). A small
retry wraps connection acquisition to tolerate transient SQL Server hiccups
(maintenance windows, brief network blips). The engine is a process singleton.
"""
from __future__ import annotations

import os
import time
import urllib.parse
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import InterfaceError, OperationalError

from core import crypto

# Transient connection errors worth retrying. Programming/auth errors are not
# in this set — those should fail fast and loud.
_RETRYABLE = (OperationalError, InterfaceError)

_engine: Optional[Engine] = None


def _build_odbc_connect_string() -> str:
    """Assemble an ODBC connection string from environment variables."""
    driver = os.environ.get("DB_DRIVER", "ODBC Driver 18 for SQL Server")
    server = os.environ.get("DB_SERVER", "localhost")
    port = os.environ.get("DB_PORT", "").strip()
    database = os.environ.get("DB_NAME", "tradebot")
    encrypt = os.environ.get("DB_ENCRYPT", "yes")
    trust = os.environ.get("DB_TRUST_SERVER_CERTIFICATE", "no")
    auth = os.environ.get("DB_AUTH", "sql").lower()

    server_value = f"{server},{port}" if port else server
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server_value}",
        f"DATABASE={database}",
        f"Encrypt={encrypt}",
        f"TrustServerCertificate={trust}",
    ]

    if auth == "windows":
        parts.append("Trusted_Connection=yes")
    else:
        user = os.environ.get("DB_USER", "")
        password = os.environ.get("DB_PASSWORD", "")
        if not user:
            raise RuntimeError("DB_AUTH=sql requires DB_USER (and DB_PASSWORD).")
        parts.append(f"UID={user}")
        parts.append(f"PWD={password}")

    return ";".join(parts)


def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine, building it on first use."""
    global _engine
    if _engine is None:
        odbc_str = _build_odbc_connect_string()
        url = "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc_str)
        # pool_pre_ping discards stale/broken connections before handing them out.
        _engine = create_engine(url, pool_pre_ping=True, future=True)
    return _engine


def connect_with_retry(max_attempts: int = 3, base_delay: float = 1.0) -> Connection:
    """Open a connection, retrying transient failures with linear backoff."""
    engine = get_engine()
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return engine.connect()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < max_attempts:
                time.sleep(base_delay * attempt)
    assert last_exc is not None
    raise last_exc


def healthcheck() -> bool:
    """Return True if a trivial query succeeds against the database."""
    with connect_with_retry() as conn:
        conn.execute(text("SELECT 1"))
    return True


def get_active_credentials(provider: str, environment: str) -> tuple[str, str]:
    """Return decrypted (api_key, api_secret) for the active row.

    Raises RuntimeError if no active row exists for the provider/environment.
    """
    sql = text(
        "SELECT TOP 1 encrypted_api_key, encrypted_api_secret "
        "FROM dbo.api_credentials "
        "WHERE provider = :provider AND environment = :environment "
        "  AND is_active = 1 "
        "ORDER BY created_at_utc DESC"
    )
    with connect_with_retry() as conn:
        row = conn.execute(
            sql, {"provider": provider, "environment": environment}
        ).fetchone()

    if row is None:
        raise RuntimeError(
            f"No active credentials found for provider='{provider}', "
            f"environment='{environment}'. Run scripts.insert_credentials first."
        )
    return crypto.decrypt(row[0]), crypto.decrypt(row[1])
