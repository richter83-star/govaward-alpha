"""SMTP email notifier."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from govaward_alpha.config import get_settings
from govaward_alpha.models import Award, Company, ScoreResult
from govaward_alpha.notify.console import render_alert

logger = logging.getLogger(__name__)


def send_alert(award: Award, company: Company | None, result: ScoreResult) -> bool:
    """Send alert via SMTP. Returns True on success."""
    settings = get_settings()
    if not settings.email_enabled:
        return False
    if not all([settings.smtp_host, settings.smtp_user, settings.email_to]):
        logger.warning("Email not configured — skipping email alert.")
        return False

    body = render_alert(award, company, result)
    subject = (
        f"[GovAward {result.level}] {award.ticker or 'UNRESOLVED'} — "
        f"{award.awardee_name_raw[:60]} — Score {result.score}/100"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.email_from or settings.smtp_user
    msg["To"] = settings.email_to
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.sendmail(msg["From"], [settings.email_to], msg.as_string())
        logger.info("Email alert sent for %s", award.ticker)
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False
