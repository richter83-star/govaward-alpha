"""Daily report generator.

Produces a Markdown report summarizing recent alert activity. Designed for
the autonomous daily-scan workflow: after the scheduled scan runs, this
service builds a human-readable digest of what's new, what changed, what
Claude thought about it, and where the source pipelines stand.

Output is written to ``logs/reports/YYYY-MM-DD.md`` by default. If email
is configured in .env, the report is also emailed to ``EMAIL_TO``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from govaward_alpha import db
from govaward_alpha.config import get_settings
from govaward_alpha.timeutil import utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportResult:
    markdown: str
    report_path: Path
    emailed: bool
    telegrammed: bool
    new_alerts_count: int
    fresh_alerts_count: int
    unresolved_high_value: int


def _fmt_amount(val: float | None) -> str:
    if val is None:
        return "N/A"
    if val >= 1e9:
        return f"${val/1e9:.2f}B"
    if val >= 1e6:
        return f"${val/1e6:.1f}M"
    if val >= 1e3:
        return f"${val/1e3:.0f}K"
    return f"${val:,.0f}"


def _fmt_pct(num: float | None, denom: float | None) -> str:
    if not num or not denom or denom == 0:
        return "N/A"
    return f"{(num/denom)*100:.1f}%"


def _safe(s: str | None, limit: int = 60) -> str:
    if not s:
        return ""
    s = str(s).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _query_new_alerts(
    conn: sqlite3.Connection,
    since: datetime,
) -> list[sqlite3.Row]:
    """Alerts created in the local DB since the given timestamp."""
    return conn.execute(
        """
        SELECT al.id as alert_id, al.materiality_score, al.alert_level,
               al.reasons_json, al.claude_analysis_json, al.created_at,
               aw.awardee_name_raw, aw.award_amount, aw.obligated_amount,
               aw.agency, aw.posted_date, aw.source, aw.source_url,
               aw.is_idiq, aw.is_modification, al.ticker,
               co.company_name, co.market_cap, co.trailing_revenue, co.sector
        FROM alerts al
        JOIN awards aw ON al.award_id = aw.id
        LEFT JOIN companies co ON al.ticker = co.ticker
        WHERE al.created_at >= ?
        ORDER BY al.materiality_score DESC, aw.award_amount DESC
        """,
        (since.isoformat(),),
    ).fetchall()


def _query_fresh_alerts(
    conn: sqlite3.Connection,
    days: int,
    min_market_cap: float,
    max_market_cap: float,
    limit: int = 20,
) -> list[sqlite3.Row]:
    """Top alerts whose award was posted within ``days`` and whose ticker
    resolves to a small/mid-cap public company."""
    return conn.execute(
        """
        SELECT al.id as alert_id, al.materiality_score, al.alert_level,
               al.claude_analysis_json,
               aw.awardee_name_raw, aw.award_amount, aw.obligated_amount,
               aw.agency, aw.posted_date, aw.source, aw.source_url,
               al.ticker, co.company_name, co.market_cap
        FROM alerts al
        JOIN awards aw ON al.award_id = aw.id
        LEFT JOIN companies co ON al.ticker = co.ticker
        WHERE aw.posted_date >= date('now', ?)
          AND co.market_cap IS NOT NULL
          AND co.market_cap BETWEEN ? AND ?
          AND al.ticker IS NOT NULL AND al.ticker != 'UNRESOLVED'
        ORDER BY aw.posted_date DESC, al.materiality_score DESC
        LIMIT ?
        """,
        (f"-{days} days", min_market_cap, max_market_cap, limit),
    ).fetchall()


def _query_unresolved_high_value(
    conn: sqlite3.Connection,
    days: int,
    min_amount: float,
    limit: int = 15,
) -> list[sqlite3.Row]:
    """Recent high-value awards that didn't resolve to any ticker — candidates
    for a manual override the operator might want to add."""
    return conn.execute(
        """
        SELECT id, awardee_name_raw, award_amount, posted_date, agency
        FROM awards
        WHERE ticker IS NULL
          AND award_amount >= ?
          AND posted_date >= date('now', ?)
        ORDER BY posted_date DESC, award_amount DESC
        LIMIT ?
        """,
        (min_amount, f"-{days} days", limit),
    ).fetchall()


def _query_source_health(conn: sqlite3.Connection, hours: int = 36) -> list[sqlite3.Row]:
    """Most recent source run per source within the lookback window."""
    return conn.execute(
        """
        SELECT source,
               MAX(started_at) as last_run,
               status,
               records_seen,
               records_inserted,
               records_updated,
               error_message
        FROM source_runs
        WHERE started_at >= datetime('now', ?)
        GROUP BY source
        ORDER BY source
        """,
        (f"-{hours} hours",),
    ).fetchall()


def _render_alert_line(r: sqlite3.Row, *, include_claude: bool = True) -> str:
    awd = _fmt_amount(r["award_amount"])
    mcap = _fmt_amount(r["market_cap"]) if "market_cap" in r.keys() else "N/A"
    ratio = (
        _fmt_pct(r["award_amount"], r["market_cap"])
        if "market_cap" in r.keys() else "N/A"
    )
    flags = []
    if "is_idiq" in r.keys() and r["is_idiq"]:
        flags.append("IDIQ")
    if "is_modification" in r.keys() and r["is_modification"]:
        flags.append("MOD")
    flags_str = f" [{','.join(flags)}]" if flags else ""

    company = (r["company_name"] if "company_name" in r.keys() else None) or r["ticker"]

    lines = [
        f"**{r['ticker']}** · {r['alert_level']} · score **{r['materiality_score']}**{flags_str}",
        f"  - **{_safe(company, 50)}** ({_safe(r['awardee_name_raw'], 70)})",
        f"  - Award {awd} · MktCap {mcap} · Awd/MC **{ratio}** · Agency: {_safe(r['agency'], 40)}",
        f"  - Posted {r['posted_date'] or 'unknown'} · "
        + (f"[source]({r['source_url']})" if r["source_url"] else f"source: {r['source']}"),
    ]

    if include_claude and "claude_analysis_json" in r.keys():
        raw = r["claude_analysis_json"]
        if raw:
            try:
                c = json.loads(raw)
                conf = c.get("resolver_confidence", "?")
                qual = c.get("contract_quality", "?")
                narr = c.get("materiality_narrative", "").strip()
                if narr:
                    lines.append(f"  - **Claude** (resolver={conf}, quality={qual}): _{narr}_")
                flags = c.get("watch_flags") or []
                if flags:
                    lines.append(
                        "  - Watch: " + "; ".join(f"⚠ {f}" for f in flags[:3])
                    )
            except Exception:
                pass
    return "\n".join(lines)


def build_report(
    lookback_hours: int = 24,
    fresh_window_days: int = 7,
) -> str:
    """Build the daily-report markdown string."""
    settings = get_settings()
    now = utcnow()
    since = now - timedelta(hours=lookback_hours)

    out: list[str] = []
    out.append(f"# GovAward Alpha — Daily Report")
    out.append(f"")
    out.append(f"_Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}_  ")
    out.append(f"_Window: last {lookback_hours} hours_  ")
    out.append(f"_Filters: award ≥ ${settings.award_min_amount/1e6:.0f}M, "
               f"market cap ${settings.market_cap_min/1e6:.0f}M – "
               f"${settings.market_cap_max/1e9:.0f}B_")
    out.append("")

    with db.get_connection() as conn:
        new_alerts = _query_new_alerts(conn, since)
        fresh = _query_fresh_alerts(
            conn,
            days=fresh_window_days,
            min_market_cap=settings.market_cap_min,
            max_market_cap=settings.market_cap_max,
        )
        unresolved = _query_unresolved_high_value(
            conn, days=fresh_window_days, min_amount=settings.award_min_amount,
        )
        sources = _query_source_health(conn)

        # ---- Headline ----------------------------------------------------
        out.append("## Summary")
        out.append("")
        out.append(f"- **{len(new_alerts)} new alert(s)** detected in the last {lookback_hours}h")
        out.append(f"- **{len(fresh)} fresh small/mid-cap signal(s)** posted in the last {fresh_window_days}d")
        out.append(f"- **{len(unresolved)} unresolved high-value award(s)** (candidates for manual override)")
        out.append("")

        # ---- New alerts since last report --------------------------------
        out.append("## New alerts (since last report)")
        out.append("")
        if not new_alerts:
            out.append("_No new alerts in this window._")
        else:
            for r in new_alerts[:25]:
                out.append(_render_alert_line(r))
                out.append("")
            if len(new_alerts) > 25:
                out.append(f"_…and {len(new_alerts) - 25} more._")
        out.append("")

        # ---- Top fresh signals across the recent window ------------------
        out.append(f"## Top fresh small/mid-cap signals (last {fresh_window_days} days)")
        out.append("")
        if not fresh:
            out.append("_No fresh small/mid-cap signals in this window. "
                       "Possible causes: weekend (no new postings); USAspending lag; "
                       "all recent awardees went to private/nonprofit contractors._")
        else:
            for r in fresh:
                out.append(_render_alert_line(r))
                out.append("")
        out.append("")

        # ---- Unresolved awards that the operator might want to map -------
        out.append("## Unresolved high-value awards")
        out.append("")
        out.append("_Recent $50M+ awards where ticker resolution failed. Add overrides via "
                   "`govaward add-override \"<awardee>\" TICKER \"<parent>\"` if any of these "
                   "are subsidiaries of public companies._")
        out.append("")
        if not unresolved:
            out.append("_None._")
        else:
            for r in unresolved:
                out.append(
                    f"- {r['posted_date']}  **{_fmt_amount(r['award_amount']):<8}** "
                    f"_{_safe(r['awardee_name_raw'], 55)}_  "
                    f"({_safe(r['agency'], 32)})"
                )
        out.append("")

        # ---- Source pipeline health --------------------------------------
        out.append("## Source pipeline health (last 36h)")
        out.append("")
        if not sources:
            out.append("_No scans recorded in the last 36 hours. The scheduled task may not "
                       "be running. Verify with `schtasks /Query /TN \"GovAwardAlpha-DailyScan\"`._")
        else:
            out.append("| Source | Last run | Status | Seen | Inserted | Updated | Error |")
            out.append("|---|---|---|---:|---:|---:|---|")
            for s in sources:
                err = (s["error_message"] or "")[:60]
                out.append(
                    f"| {s['source']} | {(s['last_run'] or '')[:16]} | "
                    f"**{s['status']}** | {s['records_seen']} | "
                    f"{s['records_inserted']} | {s['records_updated']} | {err} |"
                )
        out.append("")
        out.append("---")
        out.append("")
        out.append("_GovAward Alpha · public-information research · not investment advice._")

    return "\n".join(out)


def write_report(
    markdown: str,
    out_dir: Path | None = None,
) -> Path:
    """Write report to logs/reports/YYYY-MM-DD.md and return the path."""
    if out_dir is None:
        # Resolve relative to repo root (../../.. from this file)
        out_dir = Path(__file__).resolve().parent.parent.parent.parent / "logs" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = utcnow().strftime("%Y-%m-%d")
    path = out_dir / f"{today}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def _build_telegram_summary(markdown: str, *, max_chars: int = 3500) -> str:
    """Render a condensed summary of the report for Telegram's 4096-char limit.

    Pulls the Summary block plus the first few entries of New alerts and Top
    fresh signals. Designed to fit comfortably inside a single Telegram
    message even with Markdown formatting overhead.
    """
    lines = markdown.splitlines()
    out: list[str] = []
    in_section = None
    sections_kept = {"Summary", "New alerts", "Top fresh"}
    rows_per_section = {"New alerts": 5, "Top fresh": 5}
    rows_seen = 0

    for line in lines:
        if line.startswith("# "):
            out.append(line)
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
            in_section = next((s for s in sections_kept if heading.startswith(s)), None)
            rows_seen = 0
            if in_section:
                out.append("")
                out.append(line)
            continue
        if in_section is None:
            continue
        # Limit how many lines we keep per section to stay under the cap.
        max_rows = rows_per_section.get(in_section)
        if max_rows is None:
            out.append(line)
            continue
        # Each alert occupies ~5 lines; count "primary" lines starting with **
        if line.startswith("**") or line.startswith("- "):
            if rows_seen >= max_rows:
                continue
            rows_seen += 1
        out.append(line)

    out.append("")
    out.append("_Full report: see attached file or open logs/reports/YYYY-MM-DD.md_")
    text = "\n".join(out).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n…[truncated]"
    return text


def telegram_report(markdown: str, report_path: Path | None = None) -> bool:
    """Send the report to Telegram if TELEGRAM_ENABLED + creds are configured.

    - If the report fits in one Telegram message (≤4000 chars), sends it as
      text via ``sendMessage``.
    - Otherwise sends a condensed summary via ``sendMessage`` AND uploads the
      full Markdown as a file attachment via ``sendDocument``.

    Returns True on success, False otherwise.
    """
    import httpx

    settings = get_settings()
    if not settings.telegram_enabled:
        logger.debug("Telegram disabled — skipping Telegram send.")
        return False
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Telegram enabled but bot_token or chat_id missing — skipping.")
        return False

    # Defensive: force IPv4 — same routing issue we hit with SAM.gov on this
    # Windows host can affect api.telegram.org.
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

    short_enough = len(markdown) <= 4000
    text_payload = markdown if short_enough else _build_telegram_summary(markdown)

    # Telegram's MarkdownV2 is finicky about escaping; use plain text + a leading
    # code block for headings so it renders nicely without escaping every "."
    msg_payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text_payload,
        "disable_web_page_preview": True,
        # Note: deliberately NOT setting parse_mode — the report contains
        # underscores in dates/filenames that would break Markdown parsing.
        # Plain text renders fine on mobile.
    }

    try:
        with httpx.Client(transport=transport, timeout=20) as client:
            resp = client.post(f"{base}/sendMessage", json=msg_payload)
            resp.raise_for_status()
            logger.info("Telegram report message sent (%d chars).", len(text_payload))

            # If the report didn't fit, also upload the full file
            if not short_enough and report_path and report_path.exists():
                with open(report_path, "rb") as f:
                    files = {"document": (report_path.name, f, "text/markdown")}
                    data = {
                        "chat_id": settings.telegram_chat_id,
                        "caption": f"Full daily report — {report_path.name}",
                    }
                    resp2 = client.post(f"{base}/sendDocument", files=files, data=data)
                    resp2.raise_for_status()
                logger.info("Telegram report document attached: %s", report_path.name)
        return True
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


def email_report(markdown: str, subject_hint: str = "") -> bool:
    """Send the report via SMTP if EMAIL_ENABLED + creds are configured.

    Returns True on success, False otherwise.
    """
    settings = get_settings()
    if not settings.email_enabled:
        logger.debug("Email disabled — skipping email send.")
        return False
    if not (settings.smtp_host and settings.smtp_user and settings.email_to):
        logger.warning("Email enabled but SMTP creds incomplete — skipping send.")
        return False

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    today = utcnow().strftime("%Y-%m-%d")
    subject = f"GovAward Alpha — Daily Report {today}"
    if subject_hint:
        subject += f" — {subject_hint}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.email_from or settings.smtp_user
    msg["To"] = settings.email_to
    msg.attach(MIMEText(markdown, "plain", "utf-8"))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        logger.info("Daily report emailed to %s", settings.email_to)
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False


def generate_and_deliver(
    lookback_hours: int = 24,
    fresh_window_days: int = 7,
    send_email: bool = True,
    send_telegram: bool = True,
) -> ReportResult:
    """End-to-end: build the report, write it to disk, optionally deliver via
    email and/or Telegram."""
    md = build_report(lookback_hours=lookback_hours, fresh_window_days=fresh_window_days)
    path = write_report(md)
    emailed = email_report(md) if send_email else False
    telegrammed = telegram_report(md, report_path=path) if send_telegram else False

    # Pull a few headline counts back out for the return value
    with db.get_connection() as conn:
        since = utcnow() - timedelta(hours=lookback_hours)
        new_alerts = _query_new_alerts(conn, since)
        fresh = _query_fresh_alerts(
            conn, days=fresh_window_days,
            min_market_cap=get_settings().market_cap_min,
            max_market_cap=get_settings().market_cap_max,
        )
        unresolved = _query_unresolved_high_value(
            conn, days=fresh_window_days, min_amount=get_settings().award_min_amount,
        )

    logger.info(
        "Report written: %s (new=%d fresh=%d unresolved=%d emailed=%s telegram=%s)",
        path, len(new_alerts), len(fresh), len(unresolved), emailed, telegrammed,
    )
    return ReportResult(
        markdown=md,
        report_path=path,
        emailed=emailed,
        telegrammed=telegrammed,
        new_alerts_count=len(new_alerts),
        fresh_alerts_count=len(fresh),
        unresolved_high_value=len(unresolved),
    )
