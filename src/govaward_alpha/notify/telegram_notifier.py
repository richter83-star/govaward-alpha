"""Telegram Bot API notifier."""

from __future__ import annotations

import logging

import httpx

from govaward_alpha.config import get_settings
from govaward_alpha.models import Award, Company, ScoreResult
from govaward_alpha.notify.console import render_alert

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def send_alert(award: Award, company: Company | None, result: ScoreResult) -> bool:
    """Send alert via Telegram Bot API. Returns True on success."""
    settings = get_settings()
    if not settings.telegram_enabled:
        return False
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Telegram not configured — skipping.")
        return False

    body = render_alert(award, company, result)
    # Telegram messages max ~4096 chars
    if len(body) > 4000:
        body = body[:4000] + "\n...[truncated]"

    url = _API_BASE.format(token=settings.telegram_bot_token)
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": f"```\n{body}\n```",
        "parse_mode": "Markdown",
    }

    try:
        with httpx.Client() as client:
            resp = client.post(url, json=payload, timeout=15)
            resp.raise_for_status()
        logger.info("Telegram alert sent for %s", award.ticker)
        return True
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False
