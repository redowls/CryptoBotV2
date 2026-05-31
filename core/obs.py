"""Structured JSON logging (Phase 7, SUMMARY Observability).

Emits one JSON object per log line to a daily-rotated file (UTC midnight) plus
stderr, built on the Python stdlib only — no new runtime dependency, the same
minimal-dep ethos as the rest of the bot. ``structlog`` can be layered in later
purely as a richer renderer; the canonical record shape is owned here either way
so output stays stable across environments.

Call sites::

    log = obs.get_logger("daemon")
    log.info("cycle_start", symbols=12, dry_run=False)
    log.error("cycle_crash", error=str(exc))

The pure helpers (:func:`iso_utc`, :func:`event_dict`) are side-effect-free so
the green-light can pin the record shape without touching the filesystem, and
:func:`get_logger` defers all config/filesystem work to the first emit so simply
importing a module that holds a module-level logger never touches the DB.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

_configured = False
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _cfg(key: str, default):
    """Read an obs.* knob from app_config, falling back to a constant.

    Logging must work even when the DB is UNREACHABLE (that's exactly when we
    need to log it), so any failure resolving config degrades to the default
    rather than raising. Imported lazily to keep `import core.obs` DB-free.
    """
    try:
        from core import config  # local import: obs must not hard-depend on the DB
        if isinstance(default, int):
            return config.get_int(key, default)
        return config.get_str(key, default)
    except Exception:
        return default


# --------------------------------------------------------------------------
# Pure helpers (offline-testable)
# --------------------------------------------------------------------------
def iso_utc(dt: Optional[datetime] = None) -> str:
    """ISO-8601 UTC timestamp with a trailing 'Z' (invariant #9: all UTC)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def event_dict(event: str, level: str, *, ts: Optional[datetime] = None, **fields: Any) -> dict:
    """Build the canonical log record: ts, level, event, then caller fields.

    Insertion order is preserved so the JSON line always leads with the three
    fixed keys. Field values that are not JSON-native are coerced via ``str``.
    """
    rec: dict[str, Any] = {"ts": iso_utc(ts), "level": level.upper(), "event": event}
    for k, v in fields.items():
        rec[k] = v
    return rec


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
def configure(
    *,
    log_dir: Optional[str] = None,
    level: Optional[str] = None,
    retention_days: Optional[int] = None,
) -> None:
    """Idempotently configure the root logging handlers (JSON file + stderr).

    Reads ``obs.log_dir`` / ``obs.log_level`` / ``obs.log_retention_days`` from
    app_config when the args are omitted. Safe to call more than once.
    """
    global _configured
    if _configured:
        return

    log_dir = log_dir or _cfg("obs.log_dir", "logs")
    level_name = (level or _cfg("obs.log_level", "INFO")).upper()
    if level_name not in _LEVELS:
        level_name = "INFO"
    retention = retention_days if retention_days is not None else _cfg(
        "obs.log_retention_days", 14
    )

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "tradebot.log")

    root = logging.getLogger("tradebot")
    root.setLevel(getattr(logging, level_name))
    root.handlers.clear()
    root.propagate = False

    # Daily rotation at UTC midnight; keep `retention` days of history.
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", utc=True, backupCount=retention, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)

    _configured = True


class _Logger:
    """Thin component-bound logger: ``.info/.warning/.error/.critical(event, **fields)``.

    Renders each call as a single JSON line via :func:`event_dict`. structlog,
    when present, is used purely as the JSON renderer; the record shape is ours
    either way so output is stable across environments.
    """

    def __init__(self, component: str) -> None:
        self._component = component
        self._log = logging.getLogger("tradebot")

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        configure()  # idempotent; lazy so `get_logger` stays import-time DB-free
        rec = event_dict(event, level, component=self._component, **fields)
        try:
            line = json.dumps(rec, default=str)
        except (TypeError, ValueError):
            line = json.dumps({"ts": rec["ts"], "level": level, "event": event}, default=str)
        self._log.log(getattr(logging, level, logging.INFO), line)

    def info(self, event: str, **fields: Any) -> None:
        self._emit("INFO", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit("WARNING", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("ERROR", event, **fields)

    def critical(self, event: str, **fields: Any) -> None:
        self._emit("CRITICAL", event, **fields)


def get_logger(component: str) -> _Logger:
    """Return a component-bound JSON logger.

    Handler configuration is deferred to the first actual emit (lazy), so simply
    importing a module that holds a module-level logger never touches the DB or
    filesystem — which keeps the offline green-light and `import core.*` clean.
    """
    return _Logger(component)
