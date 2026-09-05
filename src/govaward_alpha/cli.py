"""GovAward Alpha Engine — command-line interface."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from tabulate import tabulate

app = typer.Typer(
    name="govaward",
    help="GovAward Alpha Engine — government contract award alert system.",
    add_completion=False,
)
console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("govaward_alpha.cli")


def _require_db() -> None:
    """Ensure the database has been initialised."""
    import os
    from govaward_alpha.config import get_settings
    settings = get_settings()
    if not os.path.exists(settings.db_path):
        console.print("[red]Database not found.[/red] Run [bold]govaward init-db[/bold] first.")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# init-db
# ---------------------------------------------------------------------------

@app.command("init-db")
def init_db() -> None:
    """Create the SQLite database and all required tables."""
    from govaward_alpha.db import init_db as _init_db
    _init_db()
    console.print("[green]Database initialised.[/green]")


# ---------------------------------------------------------------------------
# scan (all sources)
# ---------------------------------------------------------------------------

@app.command("scan")
def scan(
    lookback: Optional[int] = typer.Option(
        None, "--lookback", "-l", help="Days to look back (overrides LOOKBACK_DAYS)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run all data sources: SAM.gov, Defense.gov, USAspending."""
    _require_db()
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    from govaward_alpha.services.scan_service import run_all
    results = run_all(lookback_days=lookback)

    table = Table(title="Scan Summary")
    table.add_column("Source")
    table.add_column("Seen", justify="right")
    table.add_column("Inserted", justify="right")
    table.add_column("Updated", justify="right")
    table.add_column("Unchanged", justify="right")
    table.add_column("Alerted", justify="right")
    table.add_column("Errors", justify="right")

    for src, stats in results.items():
        table.add_row(
            src,
            str(stats.get("seen", 0)),
            str(stats.get("inserted", 0)),
            str(stats.get("updated", 0)),
            str(stats.get("unchanged", 0)),
            str(stats.get("alerted", 0)),
            str(stats.get("errors", 0)),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# scan-sam
# ---------------------------------------------------------------------------

@app.command("scan-sam")
def scan_sam(
    lookback: Optional[int] = typer.Option(None, "--lookback", "-l"),
) -> None:
    """Run only the SAM.gov Award Notices adapter."""
    _require_db()
    from govaward_alpha.services.scan_service import run_source
    from govaward_alpha.sources import sam_gov
    stats = run_source("sam_gov", sam_gov.fetch_awards, lookback)
    console.print(stats)


# ---------------------------------------------------------------------------
# scan-defense
# ---------------------------------------------------------------------------

@app.command("scan-defense")
def scan_defense(
    lookback: Optional[int] = typer.Option(None, "--lookback", "-l"),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="Local HTML file to parse directly"),
) -> None:
    """Run only the Defense.gov daily contracts parser."""
    _require_db()
    from functools import partial
    from govaward_alpha.services.scan_service import run_source
    from govaward_alpha.sources import defense_daily
    fetcher = partial(defense_daily.fetch_awards, file_path=file) if file else defense_daily.fetch_awards
    stats = run_source("defense_daily", fetcher, lookback)
    console.print(stats)


# ---------------------------------------------------------------------------
# scan-usaspending
# ---------------------------------------------------------------------------

@app.command("scan-usaspending")
def scan_usaspending(
    lookback: Optional[int] = typer.Option(None, "--lookback", "-l"),
) -> None:
    """Run only the USAspending.gov adapter."""
    _require_db()
    from govaward_alpha.services.scan_service import run_source
    from govaward_alpha.sources import usaspending
    stats = run_source("usaspending", usaspending.fetch_recent_awards, lookback)
    console.print(stats)


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------

@app.command("backfill")
def backfill(
    start_date: str = typer.Argument(..., help="Start date YYYY-MM-DD"),
    end_date: str = typer.Argument(..., help="End date YYYY-MM-DD"),
) -> None:
    """Backfill historical awards from USAspending for a date range."""
    _require_db()
    from govaward_alpha.sources.usaspending import backfill_awards
    from govaward_alpha.services.scan_service import run_source

    def _fetch() -> list:
        return backfill_awards(start_date, end_date)

    stats = run_source("usaspending_backfill", _fetch)
    console.print(f"Backfill complete: {stats}")


# ---------------------------------------------------------------------------
# list-alerts
# ---------------------------------------------------------------------------

@app.command("list-alerts")
def list_alerts(
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """Display recent alerts in table format."""
    _require_db()
    from govaward_alpha.db import get_connection, list_alerts as _list_alerts

    with get_connection() as conn:
        rows = _list_alerts(conn, limit)

    if not rows:
        console.print("No alerts yet. Run [bold]govaward scan[/bold] first.")
        return

    headers = ["ID", "Level", "Score", "Ticker", "Awardee", "Amount", "Agency", "Date"]
    table_data = []
    for r in rows:
        amount = r["award_amount"]
        amount_str = f"${amount/1e6:.1f}M" if amount else "N/A"
        table_data.append([
            r["id"],
            r["alert_level"],
            r["materiality_score"],
            r["ticker"],
            (r["awardee_name_raw"] or "")[:40],
            amount_str,
            (r["agency"] or "")[:30],
            (r["created_at"] or "")[:10],
        ])

    console.print(tabulate(table_data, headers=headers, tablefmt="rounded_grid"))


# ---------------------------------------------------------------------------
# explain-alert
# ---------------------------------------------------------------------------

@app.command("explain-alert")
def explain_alert(
    alert_id: int = typer.Argument(..., help="Alert ID from list-alerts"),
) -> None:
    """Show full reasoning and data for a specific alert."""
    _require_db()
    from govaward_alpha.db import get_connection, get_alert_by_id

    with get_connection() as conn:
        row = get_alert_by_id(conn, alert_id)

    if not row:
        console.print(f"[red]Alert #{alert_id} not found.[/red]")
        raise typer.Exit(1)

    reasons = json.loads(row["reasons_json"] or "[]")
    console.print(f"\n[bold]Alert #{alert_id}[/bold]")
    console.print(f"  Level:          {row['alert_level']}")
    console.print(f"  Score:          {row['materiality_score']} / 100")
    console.print(f"  Ticker:         {row['ticker']}")
    console.print(f"  Awardee:        {row['awardee_name_raw']}")
    console.print(f"  Award Amount:   {row['award_amount']}")
    console.print(f"  Obligated:      {row['obligated_amount']}")
    console.print(f"  Agency:         {row['agency']}")
    console.print(f"  Description:    {(row['description'] or '')[:300]}")
    console.print(f"  Source:         {row['source']}")
    console.print(f"  URL:            {row['source_url']}")
    console.print(f"  Posted:         {row['posted_date']}")
    console.print(f"  Channels:       console={row['sent_console']} email={row['sent_email']} "
                  f"telegram={row['sent_telegram']} discord={row['sent_discord']}")
    console.print("\n[bold]Reasons:[/bold]")
    for r in reasons:
        console.print(f"  • {r}")
    console.print()


# ---------------------------------------------------------------------------
# unresolved
# ---------------------------------------------------------------------------

@app.command("unresolved")
def unresolved(
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """List large awards that could not be mapped to a public ticker."""
    _require_db()
    from govaward_alpha.db import get_connection, list_unresolved_awards

    with get_connection() as conn:
        rows = list_unresolved_awards(conn, limit)

    if not rows:
        console.print("No unresolved awards above threshold.")
        return

    headers = ["ID", "Awardee (raw)", "Amount", "Agency", "Source", "Date"]
    table_data = []
    for r in rows:
        amount = r["award_amount"]
        amount_str = f"${amount/1e6:.1f}M" if amount else "N/A"
        table_data.append([
            r["id"],
            (r["awardee_name_raw"] or "")[:50],
            amount_str,
            (r["agency"] or "")[:30],
            r["source"],
            r["posted_date"] or "N/A",
        ])

    console.print(tabulate(table_data, headers=headers, tablefmt="rounded_grid"))
    console.print(
        f"\nUse [bold]govaward add-override[/bold] to map any of these to a ticker."
    )


# ---------------------------------------------------------------------------
# add-override
# ---------------------------------------------------------------------------

@app.command("add-override")
def add_override(
    awardee_name: str = typer.Argument(..., help="Raw awardee name (as seen in awards)"),
    ticker: str = typer.Argument(..., help="Stock ticker symbol (e.g. KTOS)"),
    parent_name: str = typer.Argument(..., help="Public parent company full name"),
) -> None:
    """Add or update a manual awardee-name → ticker mapping."""
    from govaward_alpha.enrich.company_resolver import add_override as _add_override
    _add_override(awardee_name, ticker, parent_name)
    console.print(
        f"[green]Override saved:[/green] '{awardee_name}' → {ticker.upper()} ({parent_name})"
    )


# ---------------------------------------------------------------------------
# enrich-alerts — backfill Claude analysis on existing alerts
# ---------------------------------------------------------------------------

@app.command("enrich-alerts")
def enrich_alerts(
    limit: int = typer.Option(0, "--limit", "-n", help="Max alerts to enrich (0 = all)"),
    sleep: float = typer.Option(0.5, "--sleep", help="Seconds between API calls"),
) -> None:
    """Backfill Claude analysis on alerts that don't already have it.

    Use this after first configuring ANTHROPIC_API_KEY in .env to enrich
    historical alerts that were created before the API key was set.
    """
    _require_db()
    from govaward_alpha.services.enrich_service import backfill_claude_analysis
    stats = backfill_claude_analysis(limit=limit or None, sleep_seconds=sleep)

    if stats.skipped_no_key:
        console.print("[red]ANTHROPIC_API_KEY not set.[/red] Add it to .env and retry.")
        raise typer.Exit(1)

    table = Table(title="Enrichment Backfill")
    table.add_column("Metric", justify="left")
    table.add_column("Count", justify="right")
    table.add_row("Candidates",  str(stats.candidates))
    table.add_row("Enriched",    str(stats.enriched))
    table.add_row("Failed",      str(stats.failed))
    console.print(table)


# ---------------------------------------------------------------------------
# daily-report — generate daily summary
# ---------------------------------------------------------------------------

@app.command("daily-report")
def daily_report(
    lookback_hours: int = typer.Option(24, "--hours", "-h", help="Window for 'new alerts'"),
    fresh_days: int = typer.Option(7, "--fresh-days", help="Window for 'fresh signals'"),
    email: bool = typer.Option(True, "--email/--no-email", help="Email the report if SMTP configured"),
    telegram: bool = typer.Option(True, "--telegram/--no-telegram", help="Send to Telegram if configured"),
    show: bool = typer.Option(True, "--show/--no-show", help="Print the report to stdout"),
) -> None:
    """Generate the daily report (Markdown to logs/reports/YYYY-MM-DD.md).

    Output is always written to disk. If EMAIL_ENABLED=true and SMTP creds
    are set in .env, the report is also emailed to EMAIL_TO. If
    TELEGRAM_ENABLED=true and a bot token + chat_id are configured, the
    report is also pushed to Telegram (with the full markdown attached as a
    document when it exceeds the 4000-char message limit).
    """
    _require_db()
    from govaward_alpha.services.report_service import generate_and_deliver
    result = generate_and_deliver(
        lookback_hours=lookback_hours,
        fresh_window_days=fresh_days,
        send_email=email,
        send_telegram=telegram,
    )

    if show:
        console.print(result.markdown)
        console.print()

    console.print(
        f"[green]Report written:[/green] {result.report_path}  "
        f"(new={result.new_alerts_count} fresh={result.fresh_alerts_count} "
        f"unresolved={result.unresolved_high_value} "
        f"emailed={result.emailed} telegram={result.telegrammed})"
    )


# ---------------------------------------------------------------------------
# backfill-gemini
# ---------------------------------------------------------------------------

@app.command("backfill-gemini")
def backfill_gemini_cmd(
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max alerts to enrich"),
    sleep: float = typer.Option(0.5, "--sleep", "-s", help="Seconds to sleep between calls"),
) -> None:
    """Run Google Gemini analysis backfill on alerts lacking enrichment."""
    _require_db()
    from govaward_alpha.services.enrich_service import backfill_gemini_analysis
    stats = backfill_gemini_analysis(limit=limit, sleep_seconds=sleep)
    if stats.skipped_no_key:
        console.print("[yellow]Skipped: GEMINI_API_KEY not configured in environment or .env.[/yellow]")
    else:
        console.print(f"[green]Gemini backfill complete:[/green] {stats.enriched} enriched, {stats.failed} failed out of {stats.candidates} candidates.")


@app.command("backfill-claude", hidden=True)
def backfill_claude_cmd(
    limit: Optional[int] = typer.Option(None, "--limit", "-n"),
    sleep: float = typer.Option(0.5, "--sleep", "-s"),
) -> None:
    """Legacy alias for backfill-gemini."""
    backfill_gemini_cmd(limit=limit, sleep=sleep)


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

@app.command("dashboard")
def dashboard(
    port: int = typer.Option(8050, "--port", "-p", help="Port to bind"),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind"),
) -> None:
    """Start the GovAward Alpha web dashboard."""
    _require_db()
    try:
        import uvicorn
    except ImportError:
        console.print("[red]uvicorn not installed.[/red] Run: pip install uvicorn")
        raise typer.Exit(1)

    # Run DB migration before starting the server
    from govaward_alpha.db import init_db as _init_db
    _init_db()

    console.print(f"[green]Dashboard starting[/green] → http://{host}:{port}")
    uvicorn.run(
        "govaward_alpha.dashboard.app:app",
        host=host,
        port=port,
        reload=False,
        log_level="warning",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()
