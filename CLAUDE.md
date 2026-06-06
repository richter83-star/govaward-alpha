# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The CLI is a `typer` app exposed two ways: `python -m govaward_alpha.cli <cmd>` (works without install) and `govaward <cmd>` (after `pip install -e .`).

```bash
# First-time setup (Python 3.11+)
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -e .

# Initialize SQLite DB at $DB_PATH (default: govaward.db)
python -m govaward_alpha.cli init-db

# End-to-end scan: SAM.gov + Defense.gov + USAspending → enrich → score → alert
python -m govaward_alpha.cli scan [-l/--lookback DAYS] [-v]

# Single-source scans
python -m govaward_alpha.cli scan-sam | scan-defense | scan-usaspending

# Historical USAspending backfill
python -m govaward_alpha.cli backfill 2026-01-01 2026-04-30

# Inspect results
python -m govaward_alpha.cli list-alerts [-n 50]
python -m govaward_alpha.cli explain-alert <id>
python -m govaward_alpha.cli unresolved              # large awards with no ticker
python -m govaward_alpha.cli add-override "<raw awardee>" TICKER "<parent name>"

# FastAPI dashboard (auto-runs migrations on start)
python -m govaward_alpha.cli dashboard [--port 8050] [--host 127.0.0.1]
```

### Tests

`pyproject.toml` sets `pythonpath = ["src"]`, so pytest works without install.

```bash
pytest tests/ -v                                     # full suite
pytest tests/test_scoring.py -v                      # one file
pytest tests/test_scoring.py::test_red_alert -v      # one test
pytest --cov=src --cov-report=term-missing           # coverage
```

`pytest-httpx` is used to mock external HTTP for source-adapter tests.

## Architecture

### Pipeline (one direction, no cycles)

```
sources/*.py        → list[Award]   (SAM.gov, Defense.gov, USAspending, market_data, sec_tickers)
db.upsert_award     → (id, is_new)  content-hash dedupe; existing rows with same hash short-circuit
enrich/award_enricher.enrich_award  → resolves ticker, fetches market data, returns (Award, Company|None)
scoring.score_award → ScoreResult   (score 0–100, level RED/ORANGE/YELLOW/WATCH, reasons[], breakdown{})
enrich/award_enricher.should_alert  → (bool, reason)  watchlist bypasses market_cap_max + min_score
services/alert_service.process_alert → optional Claude enrichment, persist AlertRecord, fan out to console/email/telegram/discord
```

`services/scan_service.run_source` orchestrates this loop per source; `run_all` drives the three production sources sequentially. CLI commands are thin wrappers — business logic lives in `services/`, not `cli.py`.

### Layer ownership

- **`sources/`** — adapters that hit external APIs/HTML and return `list[Award]`. Each exposes `fetch_awards(lookback_days=...)` (or equivalent). `market_data.py` and `sec_tickers.py` are sources for *enrichment*, not awards. Use `httpx` + `tenacity` retry; respect rate limits in-band (e.g. SAM.gov has body-level 429 with `nextAccessTime` returned inside a 200 response).
- **`enrich/`** — `company_resolver` (name → ticker), `award_enricher` (ticker + market data + alert decision), `claude_enricher` (optional Anthropic-based verification, never blocks the pipeline — failures are logged and skipped).
- **`scoring.py`** — pure function over `(Award, Company|None)`. No I/O. Reasons strings are user-facing.
- **`notify/`** — one module per channel. Each exports `send_alert(award, company, result) -> bool`. Console always prints; others gated by `*_ENABLED` env vars.
- **`services/`** — orchestration. `scan_service` per-source pipeline, `alert_service` persistence + dispatch.
- **`db.py`** — SQLite via stdlib `sqlite3`. WAL mode, `Row` factory. `init_db()` creates tables; `_migrate()` does additive ALTER TABLE for existing DBs (see `claude_analysis_json` example — follow this pattern, never destructive migrations).
- **`dashboard/`** — FastAPI app at `dashboard/app.py` serving `index.html` + `/api/*` JSON endpoints. Read-only over the same SQLite DB.

### Resolution priority (do not reorder without reason)

In `enrich/company_resolver.resolve()`:

1. Manual override exact match (`data/manual_ticker_overrides.yaml`, key = normalized awardee name)
2. Manual override partial (substring, key length > 6)
3. SEC `company_tickers.json` exact normalized match
4. SEC fuzzy via `rapidfuzz.WRatio` — only if normalized name ≥ 8 chars and score ≥ 92 (lower thresholds produced false positives on generic names)
5. `yfinance.Search` fallback with name-overlap guard (partial_ratio ≥ 75)
6. Unresolved (returned with `ticker=None`, surfaces in `govaward unresolved`)

`add-override` invalidates the in-process `_override_cache`. Watchlist (`data/watchlist.yaml`) is checked separately in `should_alert` to bypass the mega-cap filter for tickers the operator always wants to see.

### Data conventions

- **Awards** are deduped by `(source, source_award_id)` UNIQUE; secondary `content_hash` (in `normalize.content_hash`) decides whether an existing row needs an UPDATE — equal hash short-circuits as no-op.
- **Market data** is cached in the `companies` table for 6 hours (`enrich/award_enricher.py` checks `last_market_data_refresh`).
- **Alerts** are 1:1 with awards via `alert_exists_for_award` — re-scanning a known award never re-alerts.
- **Dollar amounts** from prose (Defense.gov press releases, etc.) go through `normalize.parse_dollar_amount` — accept `"$95M"`, `"$1.2 billion"`, `"maximum $95,000,000"`, etc.
- **Dates** stored as ISO strings in SQLite, parsed back via Pydantic `date | None`.
- **All timestamps** use `datetime.utcnow()` (naive UTC). Keep this consistent — mixing tz-aware will break the cache-age comparisons.

### Configuration

`config.Settings` is a `pydantic-settings.BaseSettings` reading `.env` with `extra="ignore"`. Access via `get_settings()` (module-level cached). Env aliases use UPPER_SNAKE (`SAM_API_KEY`, `AWARD_MIN_AMOUNT`, etc.). `data_dir` resolves `data/` relative to repo root by walking up from `src/govaward_alpha/`.

### Claude enrichment is optional and non-blocking

`enrich/claude_enricher.enrich()` runs inside `process_alert` wrapped in try/except. A `low` `resolver_confidence` is logged as a warning but the alert still fires — the design choice is to surface, not suppress. Only set `ANTHROPIC_API_KEY` (and optionally `CLAUDE_MODEL`, default `claude-haiku-4-5-20251001`) to enable.

## Project guardrails

- All data sources are official public APIs / press releases. Do not add scrapers that bypass logins, captchas, or rate limits.
- This tool is research-only. Never add code that places trades, sends orders, or otherwise acts on the alerts.
- USAspending lags SAM.gov by 1–3 days — treat it as confirmation, not the primary signal.
- IDIQ ceiling ≠ revenue. The scorer already penalizes ceiling-only awards with low obligated amounts; preserve that logic when adjusting weights.
