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
# alpaca-check
# ---------------------------------------------------------------------------

@app.command("alpaca-check")
def alpaca_check() -> None:
    """Verify Alpaca API connection, credentials, buying power, and market clock."""
    from govaward_alpha.services.alpaca_broker import AlpacaBroker
    broker = AlpacaBroker()
    if not broker.is_configured:
        console.print("[red]Alpaca API credentials are not configured in .env[/red]")
        console.print("Please set ALPACA_API_KEY, ALPACA_SECRET_KEY, and ALPACA_ENDPOINT.")
        raise typer.Exit(1)

    console.print(f"[bold]Connecting to Alpaca ({'PAPER' if broker.is_paper else 'LIVE'})...[/bold]")
    res = broker.verify_credentials()
    if not res.get("ok"):
        console.print(f"[bold red]Alpaca Verification FAILED:[/bold red] {res.get('error')}")
        raise typer.Exit(1)

    clock = broker.get_clock()
    t = Table(title="Alpaca Account Health & Readiness")
    t.add_column("Property", style="cyan")
    t.add_column("Value", style="bold green")
    t.add_row("Environment", "PAPER SIMULATION" if res.get("is_paper") else "LIVE CAPITAL")
    t.add_row("Account Number", res.get("account_number", "N/A"))
    t.add_row("Status", res.get("status", "N/A"))
    t.add_row("Buying Power", f"${res.get('buying_power', 0.0):,.2f}")
    t.add_row("Cash Balance", f"${res.get('cash', 0.0):,.2f}")
    t.add_row("Portfolio Value", f"${res.get('portfolio_value', 0.0):,.2f}")
    t.add_row("Pattern Day Trader", str(res.get("pattern_day_trader", False)))
    t.add_row("Daytrade Count", str(res.get("daytrade_count", 0)))
    t.add_row("Trading Blocked", str(res.get("trading_blocked", False)))
    t.add_row("Market Open Now", "YES" if clock.get("is_open") else "NO (Extended Hours Active)")
    console.print(t)


# ---------------------------------------------------------------------------
# trade-prune
# ---------------------------------------------------------------------------

@app.command("trade-prune")
def trade_prune() -> None:
    """Prune legacy contract and illiquid positions, refunding capital back to paper balance."""
    _require_db()
    from govaward_alpha.db import get_connection, get_trade_config
    from govaward_alpha.services.trade_engine import trade_engine
    with get_connection() as conn:
        pruned = trade_engine.prune_disqualified_positions(conn)
        cfg = get_trade_config(conn)
        console.print(f"[bold green]Pruned {len(pruned)} disqualified positions.[/bold green]")
        for p in pruned:
            console.print(f"  • Closed {p['ticker']} (#{p['id']}) - Reason: {p['exit_reason']}")
        console.print(f"Restored Paper Cash Balance: [bold cyan]${cfg.get('paper_cash_balance', 0.0):,.2f}[/bold cyan]")


# ---------------------------------------------------------------------------
# pentagon-sniper
# ---------------------------------------------------------------------------

@app.command("pentagon-sniper")
def pentagon_sniper_cmd(
    watch: bool = typer.Option(False, "--watch", "-w", help="Continuous polling during 4:55–5:30 PM ET drop window"),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Ingest local DoD contract HTML/text file"),
    lookback: int = typer.Option(1, "--lookback", "-l", help="Days of DoD articles to scan"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Evaluate alpha setups without placing real/paper orders"),
    interval: int = typer.Option(15, "--interval", "-i", help="Seconds between polls in watch mode"),
) -> None:
    """Execute the 5:00 PM ET Pentagon Contract Digest Sniper for extended-hours alpha trades."""
    _require_db()
    from govaward_alpha.db import get_connection
    from govaward_alpha.services.pentagon_sniper import pentagon_sniper
    from govaward_alpha.timeutil import get_eastern_time, get_market_session, is_pentagon_drop_window

    et = get_eastern_time()
    session = get_market_session()
    in_window = is_pentagon_drop_window()

    console.print("[bold cyan]Pentagon DoD Digest Sniper[/bold cyan]")
    console.print(f"Current Time (ET): [bold]{et.strftime('%Y-%m-%d %H:%M:%S')}[/bold]")
    console.print(f"Market Session: [bold green]{session}[/bold green]")
    window_color = "green" if in_window else "yellow"
    window_status = "YES (5:00 PM ET Drop Window Active)" if in_window else "STANDBY (Drop window: 4:55–5:30 PM ET)"
    console.print(f"Drop Window: [{window_color}]{window_status}[/{window_color}]")

    with get_connection() as conn:
        if watch:
            console.print("[bold magenta]Entering continuous watch loop... Press Ctrl+C to stop.[/bold magenta]")
            def _pulse(msg: str):
                console.print(msg)
            pentagon_sniper.watch(
                conn=conn,
                poll_interval_seconds=interval,
                dry_run=dry_run,
                file_path=file,
                on_pulse=_pulse,
            )
        else:
            console.print("Running single sniper pass...")
            res = pentagon_sniper.snipe(
                conn=conn,
                file_path=file,
                lookback_days=lookback,
                dry_run=dry_run,
            )
            t = Table(title="Pentagon Sniper Execution Report")
            t.add_column("Metric", style="cyan")
            t.add_column("Value", style="bold")
            t.add_row("Awards Detected", str(res["awards_detected"]))
            t.add_row("New Awards Inserted", str(res["inserted_awards"]))
            t.add_row("Tickers Matched", ", ".join(res["tickers_matched"][:10]) or "None")
            t.add_row("Alpha Plays Detected", str(len(res["alpha_plays"])))
            t.add_row("Orders Staged / Executed", str(len(res["orders_placed"])))
            if res.get("errors"):
                t.add_row("Errors", "\n".join(res["errors"]))
            console.print(t)

            if res["alpha_plays"]:
                console.print("[bold green]Alpha Plays Identified:[/bold green]")
                for ap in res["alpha_plays"]:
                    console.print(f"  • [bold]{ap['ticker']}[/bold] (${ap['amount']:,.0f}) - {ap['ratio_pct']:.1f}% of Cap - {ap['reason']}")



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
