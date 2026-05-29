"""Typed access to the ``app_config`` key/value table.

``app_config`` is the single source of truth for tunable runtime parameters
(SUMMARY §3). The TA engine and scans read strategy knobs from here so they can
be retuned in SQL without a code change. Values are stored as strings; this
module coerces them to the type the caller asks for and falls back to a
supplied default when a key is missing.

Values are cached for the life of the process. A long-running daemon that wants
to pick up a SQL retune mid-run should call :func:`refresh` (or restart).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text

from core.db import connect_with_retry

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}

_cache: Optional[dict[str, Optional[str]]] = None


def _load() -> dict[str, Optional[str]]:
    global _cache
    if _cache is None:
        with connect_with_retry() as conn:
            rows = conn.execute(
                text("SELECT config_key, config_value FROM dbo.app_config")
            ).fetchall()
        _cache = {row[0]: row[1] for row in rows}
    return _cache


def refresh() -> None:
    """Drop the cache so the next read re-queries ``app_config``."""
    global _cache
    _cache = None


def get_str(key: str, default: str) -> str:
    value = _load().get(key)
    return default if value is None else value


def get_int(key: str, default: int) -> int:
    value = _load().get(key)
    if value is None or value == "":
        return default
    return int(value)


def get_float(key: str, default: float) -> float:
    value = _load().get(key)
    if value is None or value == "":
        return default
    return float(value)


def get_bool(key: str, default: bool) -> bool:
    value = _load().get(key)
    if value is None or value == "":
        return default
    norm = value.strip().lower()
    if norm in _TRUE:
        return True
    if norm in _FALSE:
        return False
    raise ValueError(f"app_config[{key!r}]={value!r} is not a boolean")
