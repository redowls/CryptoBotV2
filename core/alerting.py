"""Alerting (Phase 7, SUMMARY Observability).

Delivers operational alerts to Telegram. The critical cases this exists for:
a bot that CRASHED with open positions, an Alpaca AUTH failure, or the DB being
UNREACHABLE — any of which can silently leave money exposed.

Design rules:
  * Config-driven: master switch ``alert.enabled``, channel, minimum severity,
    and the dedup window all live in app_config (tunable in SQL).
  * Severity-gated: only alerts at or above ``alert.min_severity`` are delivered.
  * Deduplicated: an identical (severity+subject) alert is suppressed within
    ``alert.dedup_secs`` so a persistent fault can't spam. Dedup state is a small
    JSON file in the log dir, so it survives across separate monitor invocations
    (cron) as well as a long-running daemon.
  * Fail-SAFE: :func:`send_alert` NEVER raises into its caller. Alerting is the
    safety net — if it breaks, it must do so quietly and let the bot continue.

Telegram creds reuse the encrypted ``api_credentials`` store (invariant #8):
provider='telegram', bot token in encrypted_api_key, chat id in
encrypted_api_secret. No plaintext token is ever written or logged.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core import config, obs
from core.db import get_active_credentials

# Severity ordering. Higher rank = more urgent.
INFO = "info"
WARNING = "warning"
CRITICAL = "critical"
_RANK = {INFO: 10, WARNING: 20, CRITICAL: 30}

_log = obs.get_logger("alerting")


@dataclass(frozen=True)
class AlertParams:
    enabled: bool
    channel: str
    min_severity: str
    dedup_secs: int
    key_environment: str
    log_dir: str

    @classmethod
    def from_config(cls) -> "AlertParams":
        return cls(
            enabled=config.get_bool("alert.enabled", True),
            channel=config.get_str("alert.channel", "telegram").lower(),
            min_severity=config.get_str("alert.min_severity", "warning").lower(),
            dedup_secs=config.get_int("alert.dedup_secs", 3600),
            key_environment=config.get_str("alert.key_environment", "live"),
            log_dir=config.get_str("obs.log_dir", "logs"),
        )


# --------------------------------------------------------------------------
# Pure helpers (offline-testable)
# --------------------------------------------------------------------------
def severity_rank(severity: str) -> int:
    """Numeric rank for a severity; unknown severities sort as WARNING."""
    return _RANK.get(severity.lower(), _RANK[WARNING])


def should_send(severity: str, min_severity: str) -> bool:
    """True if `severity` is at or above the configured floor."""
    return severity_rank(severity) >= severity_rank(min_severity)


def dedup_key(severity: str, subject: str) -> str:
    """Stable key for dedup: same severity + same subject = same alert."""
    return f"{severity.lower()}::{subject.strip()}"


def is_duplicate(last_sent_iso: Optional[str], dedup_secs: float, now: Optional[datetime] = None) -> bool:
    """True if an identical alert was sent within the dedup window."""
    if not last_sent_iso:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(last_sent_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() < dedup_secs


def format_telegram(subject: str, body: str, severity: str, host: Optional[str] = None) -> str:
    """Render the message a human reads in Telegram."""
    icon = {INFO: "ℹ️", WARNING: "⚠️", CRITICAL: "🚨"}.get(severity.lower(), "⚠️")
    head = f"{icon} [{severity.upper()}] {subject}"
    lines = [head]
    if body:
        lines.append(body)
    if host:
        lines.append(f"host: {host}")
    lines.append(f"ts: {obs.iso_utc()}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Dedup state (file-backed so it survives across cron monitor runs)
# --------------------------------------------------------------------------
def _dedup_path(log_dir: str) -> str:
    return os.path.join(log_dir, ".alert_dedup.json")


def _load_dedup(log_dir: str) -> dict:
    try:
        with open(_dedup_path(log_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_dedup(log_dir: str, state: dict) -> None:
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(_dedup_path(log_dir), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass  # dedup is best-effort; never break alerting over it


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------
def _send_telegram(message: str, env: str, *, timeout: float = 10.0) -> bool:
    """POST one message to the Telegram Bot API. Returns True on HTTP 200.

    Resolves the bot token + chat id from the encrypted credential store. Any
    failure (no creds, network, API error) returns False — the caller logs it.
    """
    token, chat_id = get_active_credentials("telegram", env)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        return 200 <= resp.status < 300


def send_alert(
    subject: str,
    body: str = "",
    *,
    severity: str = WARNING,
    host: Optional[str] = None,
    params: Optional[AlertParams] = None,
) -> bool:
    """Deliver an alert if enabled, above the severity floor, and not a dup.

    NEVER raises. Returns True only if a message was actually delivered.
    """
    try:
        params = params or AlertParams.from_config()
    except Exception as exc:  # pragma: no cover - config/db down
        _log.error("alert_config_failed", error=str(exc), subject=subject)
        return False

    if not params.enabled:
        _log.info("alert_suppressed", reason="disabled", subject=subject, severity=severity)
        return False
    if not should_send(severity, params.min_severity):
        _log.info("alert_suppressed", reason="below_min_severity", subject=subject, severity=severity)
        return False

    key = dedup_key(severity, subject)
    state = _load_dedup(params.log_dir)
    if is_duplicate(state.get(key), params.dedup_secs):
        _log.info("alert_suppressed", reason="deduped", subject=subject, severity=severity)
        return False

    message = format_telegram(subject, body, severity, host)
    delivered = False
    try:
        if params.channel == "telegram":
            delivered = _send_telegram(message, params.key_environment)
        elif params.channel == "none":
            delivered = False
        else:
            _log.error("alert_channel_unknown", channel=params.channel, subject=subject)
    except Exception as exc:
        _log.error("alert_delivery_failed", channel=params.channel, error=str(exc), subject=subject)
        delivered = False

    if delivered:
        state[key] = obs.iso_utc()
        _save_dedup(params.log_dir, state)
        _log.warning("alert_sent", subject=subject, severity=severity, channel=params.channel)
    else:
        _log.warning("alert_not_delivered", subject=subject, severity=severity, channel=params.channel)
    return delivered
