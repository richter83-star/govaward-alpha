# GovAward Alpha Engine

A local-first alert engine that detects newly posted U.S. government contract awards of $50M+ to small/mid-cap publicly traded companies, scores their potential materiality, and sends alerts across console, email, Telegram, and Discord.

**This tool is for public-information market research only. It does not execute trades.**

---

## What This Tool Does

1. **Fetches** recent government contract award announcements from:
   - [SAM.gov](https://sam.gov) Award Notices API (near real-time)
   - [Defense.gov](https://www.defense.gov/News/Contracts/) daily contracts press releases
   - [USAspending.gov](https://api.usaspending.gov) API (confirmation/enrichment)

2. **Filters** for awards ≥ $50M (configurable).

3. **Resolves** awardee company names to public stock tickers via:
   - Manual override YAML (highest priority)
   - SEC company_tickers.json (exact + fuzzy match)
   - yfinance search fallback

4. **Enriches** resolved tickers with live market data (market cap, revenue, volume).

5. **Scores** each award 0–100 based on materiality ratios (award ÷ market cap, obligated ÷ market cap, award ÷ revenue) plus bonuses and penalties.

6. **Alerts** via console panel, and optionally email/Telegram/Discord.

7. **Stores** all awards, companies, and alerts in a local SQLite database.

---

## Core Thesis

> A $50M+ award to a mega-cap is noise. The same award to a small/mid-cap may be material.

Score logic penalizes IDIQ ceiling-only awards (ceiling ≠ revenue), modifications, and mega-caps. It rewards small-cap companies, real obligated amounts, new awards, defense/AI/cyber/space keywords, and low average trading volume (less analyst coverage).

---

## Legal / Public Data Guardrail

- All data is from official U.S. government public APIs and press releases.
- No authentication circumvention. No login scraping. No rate limit bypassing.
- Market data is from Yahoo Finance (public).
- **This is not investment advice. It does not execute trades. Verify all findings independently.**

---

## Setup

### Prerequisites

- Python 3.11+
- A free [SAM.gov API key](https://sam.gov/content/entity-registration) (takes ~5 minutes)

### Install

```bash
git clone <your-repo>
cd govaward-alpha

# Create virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -e .
```

Or without editable install:
```bash
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
```

Edit `.env`:
```
SAM_API_KEY=your_actual_key_here
AWARD_MIN_AMOUNT=50000000
LOOKBACK_DAYS=3
MARKET_CAP_MIN=50000000
MARKET_CAP_MAX=10000000000
ALERT_MIN_SCORE=70
```

### Get a SAM.gov API Key

1. Go to [https://sam.gov](https://sam.gov)
2. Create a free account (or log in)
3. Go to **Account Settings → API Key Management**
4. Generate a key and paste it into `.env` as `SAM_API_KEY`

SAM.gov API is free. Rate limits are generous for personal/research use.

---

## Running

### Initialize database (first time only)

```bash
python -m govaward_alpha.cli init-db
```

### Run a full scan

```bash
python -m govaward_alpha.cli scan
```

This fetches from SAM.gov, Defense.gov, and USAspending, enriches tickers and market data, scores, and fires alerts.

### Run individual sources

```bash
python -m govaward_alpha.cli scan-sam
python -m govaward_alpha.cli scan-defense
python -m govaward_alpha.cli scan-usaspending
```

### Backfill historical awards

```bash
python -m govaward_alpha.cli backfill 2026-01-01 2026-04-30
```

### List recent alerts

```bash
python -m govaward_alpha.cli list-alerts
python -m govaward_alpha.cli list-alerts --limit 100
```

### Explain an alert

```bash
python -m govaward_alpha.cli explain-alert 7
```

### Show unresolved awards

```bash
python -m govaward_alpha.cli unresolved
```

### Add a ticker override

```bash
python -m govaward_alpha.cli add-override "Fictional Defense Corp LLC" FICT "Fictional Corporation"
```

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Scheduling

### Linux/macOS (cron)

Run every 4 hours:
```
0 */4 * * * cd /path/to/govaward-alpha && .venv/bin/python -m govaward_alpha.cli scan >> logs/cron.log 2>&1
```

### Windows Task Scheduler

1. Open Task Scheduler → Create Basic Task
2. Set trigger: Daily, repeat every 4 hours
3. Action: Start a program → `C:\path\to\.venv\Scripts\python.exe`
4. Arguments: `-m govaward_alpha.cli scan`
5. Start in: `C:\path\to\govaward-alpha`

---

## Optional Notifications

### Email

```
EMAIL_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your_app_password
EMAIL_FROM=you@gmail.com
EMAIL_TO=you@gmail.com
```

Use a Gmail [App Password](https://myaccount.google.com/apppasswords) (not your regular password).

### Telegram

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → get `BOT_TOKEN`
2. Message [@userinfobot](https://t.me/userinfobot) → get your `CHAT_ID`

```
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=1234567890:ABCxyz...
TELEGRAM_CHAT_ID=123456789
```

### Discord

1. Server Settings → Integrations → Webhooks → New Webhook → Copy URL

```
DISCORD_ENABLED=true
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

---

## Ticker Override File

`data/manual_ticker_overrides.yaml` — maps normalized awardee names to tickers.

Example:
```yaml
overrides:
  "kratos defense security solutions":
    ticker: "KTOS"
    parent_company_name: "Kratos Defense & Security Solutions, Inc."
```

Keys are lowercase, punctuation-stripped. They take priority over all automated resolution.

Run `govaward unresolved` to find awards that need manual mapping.

---

## Watchlist

`data/watchlist.yaml` — tickers to always alert on regardless of market cap ceiling:

```yaml
tickers:
  - KTOS
  - PLTR
  - RCAT
```

---

## Alert Format

```
RED ALERT: Possible Material Federal Award

Ticker:          KTOS
Company:         Kratos Defense & Security Solutions, Inc.
Awardee:         KRATOS DEFENSE & SECURITY SOLUTIONS INC
Award Amount:    $95.0M
Obligated Now:   $18.0M
Market Cap:      $1.2B
Award / Mkt Cap: 7.9%
Oblg / Mkt Cap:  1.5%
Award / Revenue: 14.2%
Agency:          Department of Defense / Navy
Type:            Award Notice / IDIQ
Posted Date:     2026-05-02
Award Date:      2026-05-02
Source:          defense_daily
URL:             https://www.defense.gov/News/...

Score:           82 / 100
Level:           RED

Reasons:
  - Award is 7.9% of market cap (≥5%).
  - Obligated amount is 1.5% of market cap (≥2%).
  - Award is 14.2% of trailing revenue (≥10%).
  - Company is small/micro-cap (< $2B market cap).
  - New award, not a modification or option exercise.
  - Defense, AI, cyber, or space contract category detected.

Caution:
  This alert is public-information research, not a trade instruction.
  Verify contract economics, obligation timing, liquidity, SEC filings,
  and company disclosure before acting.
```

---

## Alert Levels

| Level | Score | Meaning |
|-------|-------|---------|
| RED | ≥ 80 | Potentially very material |
| ORANGE | ≥ 65 | Likely noteworthy |
| YELLOW | ≥ 50 | Worth investigating |
| WATCH | < 50 | Below threshold but above min amount |

---

## USAspending Implementation Note

The official [USAspending API backend](https://github.com/fedspendingtransparency/usaspending-api)
is open source (Django/Postgres/Elasticsearch), but this project consumes the public
production API at `https://api.usaspending.gov` rather than embedding or running the
full backend. See `docs/references.md` for all endpoint references.

---

## Known Limitations

- **USAspending may lag** 1–3 days behind SAM.gov for new awards — use it as confirmation.
- **DoD announcement parsing is best-effort** — Defense.gov pages are prose and format varies.
- **Subsidiary-parent mapping is imperfect** — many government contractors use subsidiary names; add overrides manually as you discover gaps.
- **Contract ceiling ≠ immediate revenue** — IDIQ/task order ceilings are maximum values, not guaranteed revenue. The scoring engine penalizes these but cannot fully account for contract economics.
- **Market data provider (yfinance) may be incomplete** — some OTC or foreign-listed tickers may return no data.
- **SAM.gov rate limits** — the API is free but rate-limited; the engine retries gracefully.
- **This is not investment advice and does not execute trades.**
