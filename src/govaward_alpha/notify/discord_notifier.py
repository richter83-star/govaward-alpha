"""Discord webhook notifier."""

from __future__ import annotations

import logging

import httpx

from govaward_alpha.config import get_settings
from govaward_alpha.models import Award, Company, ScoreResult
from govaward_alpha.notify.console import render_alert

logger = logging.getLogger(__name__)

_COLOR_MAP = {
    "RED": 0xFF0000,
    "ORANGE": 0xFF8C00,
    "YELLOW": 0xFFD700,
    "WATCH": 0x00BFFF,
}


def send_alert(award: Award, company: Company | None, result: ScoreResult) -> bool:
    """Send alert via Discord webhook. Returns True on success."""
    settings = get_settings()
    if not settings.discord_enabled:
        return False
    if not settings.discord_webhook_url:
        logger.warning("Discord webhook URL not configured — skipping.")
        return False

    body = render_alert(award, company, result)
    color = _COLOR_MAP.get(result.level, 0xAAAAAA)

    # Discord embed description max 4096
    if len(body) > 4000:
        body = body[:4000] + "\n...[truncated]"

    payload = {
        "embeds": [
            {
                "title": f"{result.level} ALERT — {award.ticker or 'UNRESOLVED'} — Score {result.score}/100",
                "description": f"```\n{body}\n```",
                "color": color,
            }
        ]
    }

    try:
        with httpx.Client() as client:
            resp = client.post(settings.discord_webhook_url, json=payload, timeout=15)
            resp.raise_for_status()
        logger.info("Discord alert sent for %s", award.ticker)
        return True
    except Exception as exc:
        logger.error("Discord send failed: %s", exc)
        return False
