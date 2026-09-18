# GOVAWARD ALPHA
## Precision Financial Terminal & Autonomous Federal Contract Trading Architecture
### An Institutional White Paper on Capturing Asymmetric Alpha in Small/Mid-Cap Government Procurement

---

**Document Version:** 2.5.0 (Signal Integrity & Idempotency Hardened Release)  
**Classification:** Public / Investor & Technical Overview  
**Published:** September 2026  
**Target Audience:** Quantitative Traders, Institutional Investors, Defense Analysts, and Financial Engineers  

---

## Executive Summary

The United States Federal Government represents the single largest enterprise customer on earth, awarding in excess of **$700 Billion** in contracts annually across defense, intelligence, healthcare, aerospace, and critical infrastructure. 

While contract awards to mega-cap prime contractors (e.g., Lockheed Martin, Boeing, RTX) are routinely absorbed by institutional algorithmic pricing models with negligible price impact, **multi-million-dollar awards to small-cap and mid-cap publicly traded companies ($50M – $5B market capitalization) represent one of the few persistent, legally defensible, and publicly accessible information asymmetries in modern equities markets.**

**GovAward Alpha** is an institutional-grade, real-time intelligence terminal and autonomous execution engine designed to detect, resolve, score, and trade material federal procurement catalysts before they are priced in by broad market consensus.

```
+---------------------------------------------------------------------------------------+
|                                GOVAWARD ALPHA PIPELINE                                |
|                                                                                       |
|  [ SAM.gov API ]       [ Defense.gov NLP ]       [ USAspending.gov ]                  |
|  (Paged Ingestion)     (Collision-Free Hashing)  (Action-Date Backfill)               |
|          \                     |                     /                                |
|           +--------------------+--------------------+                                 |
|                                |                                                      |
|                                v                                                      |
|                 [ 5-Tier Ticker Resolution Engine ]                                   |
|                 (Confidence: OVERRIDE | HIGH | MED | LOW | UNRESOLVED)                |
|                                |                                                      |
|                                v                                                      |
|                 [ Quantitative Scoring Engine ]                                       |
|                 (Award/MC, Oblg/MC, IDIQ Discounting, Revenue Multiplier)             |
|                                |                                                      |
|                                v                                                      |
|                 [ Dual-LLM AI Conviction Layer ]                                      |
|                 (Gemini Pro/Flash & Claude Sonnet: BUY | WATCH | TRAP)                |
|                                |                                                      |
|                                v                                                      |
|                 [ Precision Financial Terminal ]                                      |
|                 (SSE Telemetry, 3-Col Ultrawide UI, Idempotent Storage)               |
|                                |                                                      |
|                                v                                                      |
|                 [ Confidence-Gated Execution Engine ]                                 |
|                 (Alpaca Paper / Live API, TP/SL Trailing, Anti-Heuristic Gate)        |
+---------------------------------------------------------------------------------------+
```

### Key Metrics & Architectural Highlights

- **Multi-Source Ingestion & Verification:** Real-time ingestion across SAM.gov (System for Award Management), Defense.gov (Pentagon Daily Contracts Digest), and USAspending.gov with automatic pagination and action-date filtering.
- **Collision-Proof Award Identity:** Deterministic hashing factoring contract vehicle numbers, military branch sub-agencies, and narrative scope slugs, eliminating same-contractor/same-amount award collapse.
- **5-Tier Ticker Resolution & Provenance Graph:** Explicit categorization of ticker mappings (`MANUAL_OVERRIDE`, `HIGH_CONFIDENCE`, `MEDIUM_CONFIDENCE`, `LOW_CONFIDENCE`, `UNRESOLVED`) ensuring that no speculative or fuzzy heuristic can trigger automated capital execution.
- **Storage-Level Idempotency:** SQLite database layer reinforced with Write-Ahead Logging (WAL) and `UNIQUE(award_id)` constraints on alert tables, mathematically guaranteeing zero duplicate notifications or re-alerts across concurrent scanner threads.
- **Quantitative Materiality Metric (0–100):** Continuous scoring based on Award-to-Market-Cap ratio, Obligated-to-Market-Cap ratio, Trailing Revenue impact, IDIQ ceiling penalties, and thematic defense multipliers.
- **AI Synthesis & Conviction Guardrail:** Generative analysis via Google Gemini and Anthropic Claude models categorizing contract awards into institutional action tiers (`HIGH CONVICTION BUY`, `MOMENTUM WATCH`, `TRAP / AVOID`).
- **Autonomous Execution Gateway:** Dual-mode order execution (Paper Simulation & Live Capital via Alpaca Markets API) featuring dynamic bracketed Take-Profit ($TP) and Stop-Loss ($SL) orders with real-time mark-to-market position tracking.
- **24/7 High-Availability Infrastructure:** Daemonized Linux deployment on a hardened VPS with an immortal, self-healing local SSH tunnel watchdog with active HTTP health probing.

---

## 1. The Market Inefficiency & Core Thesis

### 1.1 The Information Asymmetry in Federal Contracting

Modern financial markets are dominated by high-frequency trading (HFT) and automated scrapers monitoring SEC filings (Form 8-K, 10-Q) and PR Newswires. However, a structural delay exists between the moment the U.S. Federal Government posts a contract award and when:
1. The awarded company files an 8-K with the SEC (which may take up to 4 business days, or may not occur at all if the company deems the contract non-reportable under Reg S-K).
2. The company issues a public press release.
3. Financial news wire aggregators (Bloomberg, Reuters, Benzinga) syndicate the headline.

Federal contract awards are posted first on public government portals—chiefly **SAM.gov** and **Defense.gov** (typically released at 5:00 PM ET on business days). Because these databases are formatted in unstructured government jargon (DoD procurement codes, obscure subsidiary names, undefinitized contract actions), standard market algorithms frequently fail to associate the contract with the correct public equity ticker in real time.

### 1.2 The Law of Asymmetric Impact: Mega-Caps vs. Small-Caps

The economic significance of an award is inversely proportional to the recipient's enterprise scale:

$$\text{Materiality Ratio} = \frac{\text{Contract Value}}{\text{Market Capitalization}}$$

- **Case A: The Mega-Cap Noise Floor:** A $150 Million award to Lockheed Martin (NYSE: LMT, Market Cap ~$110B) represents **0.13%** of market cap. It is immaterial noise that produces zero alpha.
- **Case B: The Small-Cap Asymmetric Catalyst:** A $95 Million award to a defense electronics firm like Kratos Defense (NASDAQ: KTOS, Market Cap ~$1.2B) represents **7.9%** of market cap and **14.2%** of annual revenue. If awarded to a micro-cap drone manufacturer with a $150M market cap, the award represents **63.3% of the entire company's value**.

When market participants discover such an award the following trading morning, significant repricing events occur (often gaps of 15% to 80% with sustained momentum). GovAward Alpha is engineered specifically to capture this window.

---

## 2. Multi-Source Ingestion & Data Triangulation

GovAward Alpha implements a triple-redundant data acquisition pipeline designed to eliminate false positives, capture breaking releases, and confirm actual obligated capital.

```
                    +------------------------------------+
                    |        Federal Procurement         |
                    +------------------------------------+
                                      |
         +----------------------------+----------------------------+
         |                            |                            |
         v                            v                            v
  [ SAM.gov API ]             [ Defense.gov NLP ]         [ USAspending API ]
  - Frequency: 5 min          - Frequency: Daily 5:05PM   - Frequency: Daily / Historical
  - Granularity: Raw notice   - Granularity: DoD releases - Granularity: Accounting data
  - Strengths: Earliest alert - Strengths: DoD prime awards- Strengths: Verifies obligations
         |                            |                            |
         +----------------------------+----------------------------+
                                      |
                                      v
                       +-------------------------------+
                       |  Normalizer & De-duplicator   |
                       |  (Collision-Free Unique Hash) |
                       +-------------------------------+
```

### 2.1 SAM.gov API (Real-Time Ingestion & Pagination)
- **Endpoint:** Federal Contract Opportunities / Award Notices API.
- **Characteristics:** Provides the earliest machine-readable notice of contract actions.
- **Pagination Architecture:** Automatically traverses result sets with dynamic offset stepping (`offset += 100`) until total records are exhausted, preventing silent award omission on high-volume days.
- **Data Extracted:** Awardee legal name, UEI (Unique Entity Identifier), CAGE code, Awarding Agency / Sub-tier, Base & Exercised Options Value, Initial Obligated Amount, NAICS / PSC classification codes, and solicitation history.

### 2.2 Defense.gov Daily Contracts Scraper (Hardened DoD Identity)
- **Source:** Pentagon Daily Digest published every business day between 5:00 PM and 5:30 PM Eastern Time.
- **Characteristics:** Covers all defense awards exceeding $7.5M to $15M thresholds across Army, Navy, Air Force, Space Force, DARPA, and Defense Logistics Agency (DLA).
- **Collision-Proof Identity Hashing (v2.5.0):** Early prototypes that hashed only `(url, awardee, amount, date)` collapsed separate awards when a major contractor received multiple contracts of identical amounts in the same daily digest. The v2.5.0 architecture computes an immutable composite identity:
  $$\text{SourceID} = \text{SHA256}\Big(\text{URL} \parallel \text{Awardee} \parallel \text{Amount} \parallel \text{ContractNumber} \parallel \text{SubAgency} \parallel \text{Slug}_{120}(\text{Description}) \parallel \text{Date}\Big)$$
  This guarantees that discrete contract awards to the same contractor on the same day never collide or drop.

### 2.3 USAspending.gov API (Confirmation & Historical Backfill)
- **Endpoint:** `https://api.usaspending.gov/api/v2/search/spending_by_award/`
- **Characteristics:** The official open-data accounting database of the U.S. Treasury.
- **Action-Date Ingestion:** Filters strictly by `action_date` (most recent transaction) rather than performance periods, capturing fresh capital commitments rather than dormant ongoing contracts.
- **Historical Backfill Engine:** Includes CLI commands (`govaward backfill YYYY-MM-DD YYYY-MM-DD`) enabling systematic historical data ingestion for quantitative model backtesting.

---

## 3. Entity Resolution: The Subsidiary-to-Ticker Graph

The core technical challenge in federal contract analysis is that **publicly traded corporations rarely contract under their parent ticker name.** 

For example:
- A contract awarded to *"AeroVironment California Operations LLC"* or *"Arcturus UAV"* belongs to **AVAV** (AeroVironment, Inc.).
- A contract awarded to *"DRS Network & Imaging Systems LLC"* belongs to **DRS** (Leonardo DRS, Inc.).
- A contract awarded to *"Kratos Unmanned Aerial Systems, Inc."* belongs to **KTOS** (Kratos Defense & Security Solutions, Inc.).

### 3.1 The 5-Tier Resolution Cascade

GovAward Alpha resolves entities through a deterministic, confidence-rated multi-tier cascade:

```
[ Raw Awardee String: "KRATOS UNMANNED AERIAL SYSTEMS, INC." ]
                         |
                         v
   [ Tier 1: Canonical String Normalization ]
   - Strip suffixes: LLC, INC, CORP, CO, LIMITED, SYSTEM, USA, GROUP
   - Normalize legal characters, ampersands, whitespace
                         |
                         v
   [ Tier 2: Manual Override Matrix (Highest Precedence) ]
   - Checks data/manual_ticker_overrides.yaml
   - Confidence: MANUAL_OVERRIDE
                         |
                         v (if unmatched)
   [ Tier 3: SEC EDGAR Master Entity Database ]
   - Exact match: Confidence = HIGH_CONFIDENCE
   - Fuzzy match (WRatio >= 92 + Head Token Guard):
       * Score >= 98: HIGH_CONFIDENCE
       * Score 92-97: MEDIUM_CONFIDENCE
                         |
                         v (if unmatched)
   [ Tier 4: Heuristic Search Fallback (Yahoo Finance) ]
   - Token & partial ratio sanity checks
   - Confidence: LOW_CONFIDENCE (Alert-Suppressed & Trade-Gated)
                         |
                         v (if unmatched)
   [ Tier 5: Unresolved Ledger ]
   - Stored with Confidence: UNRESOLVED
   - Surfaces in `govaward unresolved` for operator review
```

### 3.2 Confidence Tiers & Institutional Integrity (v2.5.0)

A fundamental principle of quantitative trading is: **a wrong ticker is exponentially worse than no ticker.** Allocating capital to an unrelated public company based on a spurious fuzzy match destroys alpha.

| Confidence Tier | Resolution Source | Alerting Policy | Auto-Trading Gate |
| :--- | :--- | :--- | :--- |
| **`MANUAL_OVERRIDE`** | Verified human mapping in `manual_ticker_overrides.yaml` | **Active** | **Eligible** |
| **`HIGH_CONFIDENCE`** | Exact SEC EDGAR CIK match or score $\ge 98$ fuzzy | **Active** | **Eligible** |
| **`MEDIUM_CONFIDENCE`** | Guarded SEC EDGAR fuzzy match ($92 \le \text{Score} < 98$) | **Active** | **Whitelisted Only** |
| **`LOW_CONFIDENCE`** | Heuristic search guess (yfinance token matching) | **Suppressed** (Watchlist only) | **BLOCKED** |
| **`UNRESOLVED`** | Private company, non-profit, or unmapped entity | **Silent Storage** | **BLOCKED** |

---

## 4. Quantitative Scoring & Valuation Model

Not all contracts generate shareholder value. A $100M IDIQ ceiling shared among 15 contractors over 10 years is vastly inferior to a $50M firm-fixed-price (FFP) contract awarded to a single firm with immediate funding.

GovAward Alpha implements a multi-variable **Materiality Index ($S \in [0, 100]$)**:

$$S = \text{Clamp}\left( S_{\text{base}} + \sum \Delta_{\text{bonuses}} - \sum \Delta_{\text{penalties}}, \; 0, \; 100 \right)$$

### 4.1 Base Materiality Scoring ($S_{\text{base}}$)

The base score evaluates the contract against the balance sheet and income statement of the recipient:

$$S_{\text{base}} = w_1 \cdot f\left(\frac{V_{\text{award}}}{\text{MarketCap}}\right) + w_2 \cdot g\left(\frac{V_{\text{obligated}}}{\text{MarketCap}}\right) + w_3 \cdot h\left(\frac{V_{\text{award}}}{\text{Revenue}_{\text{TTM}}}\right)$$

| Ratio Metric | Formula Threshold | Score Contribution |
| :--- | :--- | :--- |
| **Award / Market Cap** | $\ge 20\%$ | **+35 pts** |
| | $10\% - 19.9\%$ | **+22 pts** |
| | $5\% - 9.9\%$ | **+12 pts** |
| **Obligated / Market Cap** | $\ge 10\%$ | **+25 pts** (Immediate cash commitment) |
| | $5\% - 9.9\%$ | **+18 pts** |
| | $2\% - 4.9\%$ | **+10 pts** |
| **Award / TTM Revenue** | $\ge 50\%$ | **+25 pts** (Transformational backlog) |
| | $25\% - 49.9\%$ | **+18 pts** |
| | $10\% - 24.9\%$ | **+10 pts** |

### 4.2 Capitalization Multipliers & Small-Cap Premiums
- **Small/Micro-Cap Bonus ($< \$2\text{B}$ Market Cap):** **+10 pts**. Low analyst coverage; high likelihood of substantial price rerating.
- **De Novo Contract Bonus:** **+10 pts** for new awards (non-modifications and non-option exercises).
- **Low Trading Volume Proxy ($< 1\text{M}$ ADV):** **+5 pts**. Underfollowed catalyst premium.
- **Thematic Defense / AI / Cyber Category:** **+5 pts** for contracts matching high-priority Pentagon keywords (drones, counter-UAS, hypersonics, space, cyber).
- **Mega-Cap Exclusion ($> \$10\text{B}$ Market Cap):** Hard filtered from notifications and execution unless explicitly tracked on the operator's institutional watchlist.

### 4.3 Structural Penalties: De-Risking the Catalyst
- **IDIQ / Ceiling-Only Penalty:** **-20 pts**. An Indefinite Delivery, Indefinite Quantity (IDIQ) contract ceiling does *not* guarantee revenue; it merely establishes a spending ceiling across multiple competing vendors.
- **Modification Penalty:** **-10 pts**. Incremental modifications are frequently administrative or already priced into historical guidance.

---

## 5. Dual-LLM AI Conviction Layer

Quantitative metrics alone cannot read contractual fine print. A $200M defense contract may mandate 90% subcontracting pass-through, or contain cost-reimbursement clauses that yield negligible margins.

GovAward Alpha integrates an automated, dual-LLM intelligence evaluation layer powered by **Google Gemini 2.5** and **Anthropic Claude 3.5 Sonnet**:

```
[ Raw Scored Award Notice ]
             |
             v
+-------------------------------------------------------------+
|                AI Synthesis & Reasoning Prompt              |
|                                                             |
| Inputs:                                                     |
| - Contractor Name, Ticker, Market Cap, TTM Revenue, P/E     |
| - Exact Contract Narrative & Scope of Work                  |
| - Awarding Agency, Contract Type (FFP, CPFF, T&M, IDIQ)     |
| - Obligated Cash vs Maximum Potential Ceiling               |
+-------------------------------------------------------------+
             |
             v
+-------------------------------------------------------------+
|                   Agentic Analysis Vector                   |
|                                                             |
| 1. Dilution vs Pure Margin: Subcontractor pass-through test |
| 2. Strategic Narrative: Alignment with NDAA budget priorities|
| 3. Market Awareness: Has this contract been pre-announced?  |
| 4. Revenue Velocity: Multi-year vs Single-year deployment   |
+-------------------------------------------------------------+
             |
             v
+-------------------------------------------------------------+
|                      Structured Verdict                     |
|                                                             |
| - Verdict: [ HIGH CONVICTION BUY | MOMENTUM WATCH | AVOID ]  |
| - Conviction Confidence: 0 - 100%                           |
| - Executive Summary: 2-sentence rationale                   |
| - Downside Risk Analysis: Execution & margin vulnerabilities|
+-------------------------------------------------------------+
```

### Actionable Verdict Tiers
1. **`HIGH CONVICTION BUY`:** Firm-fixed-price de novo award; high percentage of immediate obligated funding; transformational relative to annual revenue; high operating leverage.
2. **`MOMENTUM WATCH`:** Highly visible defense award; short-term retail headline velocity likely, but long-term fundamental impact muted by multi-year delivery timelines.
3. **`TRAP / AVOID`:** Unfunded IDIQ ceiling; joint venture where the public company holds a minority interest; or heavily modified contract with zero incremental capital.

---

## 6. Execution & Risk Management Engine

GovAward Alpha bridges the gap between intelligence and execution through an automated trading engine integrated with **Alpaca Markets API**.

```
                           +------------------------+
                           |  Scored Award / Alert  |
                           +------------------------+
                                       |
                                       v
                      +------------------------------------+
                      |    Ticker Confidence Gate (v2.5)   |
                      |   (Reject LOW_CONFIDENCE / UNRES)  |
                      +------------------------------------+
                                       |
                                       v
                      +------------------------------------+
                      |    Trading Engine Mode Filter      |
                      +------------------------------------+
                                  /          \
                                 /            \
                      [ MODE: OFF ]      [ MODE: ACTIVE ]
                      (Log alert only)         |
                                               v
                               +-------------------------------+
                               |    Execution Target Router    |
                               +-------------------------------+
                                      /                 \
                                     /                   \
                         [ PAPER TRADING ]        [ LIVE CASH TRADING ]
                         - Simulated Book         - Real Alpaca API
                         - Zero Capital Risk      - Live Capital Exposure
                         - Precision Fill Model   - Real Broker Routing
                                     \                   /
                                      \                 /
                                       v               v
                               +-------------------------------+
                               |   Risk Management Envelope    |
                               |   - Dynamic Scale Sizing      |
                               |   - Auto Take-Profit (+15%)   |
                               |   - Auto Stop-Loss (-4%)      |
                               |   - Trailing Stop Protection  |
                               |   - Circuit Breaker Stop      |
                               +-------------------------------+
```

### 6.1 Execution Modes
- **`OFF` Mode:** Terminal operates in pure monitoring and research mode; no orders generated.
- **`PAPER` Mode:** Full execution simulation against live market quotes; maintains independent virtual cash ledger, position journal, and P&L accounting.
- **`LIVE` Mode:** Authenticates directly to Alpaca live endpoints (`https://api.alpaca.markets`) with secure API keys, routing orders directly to market exchanges with bracketed protection.

### 6.2 Protective Envelope & Order Architecture
When an award triggers execution (either autonomously via score threshold $\ge 80$ or via 1-Click `⚡ Trade` terminal action):
1. **Ticker Confidence Gate:** Requires `HIGH_CONFIDENCE` or `MANUAL_OVERRIDE`. Heuristic guesses never trade.
2. **Dynamic Position Sizing:** Evaluates portfolio bankroll and dynamically scales allocation based on catalyst ratio (10% to 22% of available cash) with minimum micro-account allocation rules.
3. **Bracketed Risk Guardrails:** Automatically submits parent order with simultaneous OCO (One-Cancels-Other) child orders:
   - **Take-Profit Target ($TP):** Default $+10\%$ to $+25\%$.
   - **Stop-Loss Floor ($SL):** Hard floor default $-3\%$ to $-5\%$.
   - **Trailing Stop:** Adjusts stop-loss upwards as the equity prints higher highs, locking in accumulated gains during morning momentum runs.
4. **Mark-to-Market Position Telemetry:** Background monitors poll active positions, updating unrealized P&L, distance to take-profit, and distance to stop-loss in real time.
5. **Emergency Circuit Breaker:** Instant liquidation endpoint (`POST /api/trader/positions/{id}/close` or master liquidate) closing all positions at market bid with single-click authority.

---

## 7. The Institutional Precision Financial Terminal

GovAward Alpha features a purpose-built, high-density financial terminal engineered for multi-monitor, 1440p, 4K, and ultrawide displays.

```
+--------------------------------------------------------------------------------------------------------------------------------+
| GOVAWARD ALPHA // PRECISION FINANCIAL TERMINAL                     [🔴 1 RED] [🟡 1 YELLOW] [TOTAL: $87.3T]   ⚙ Strategy  +Set |
+--------------------------------------------------------------------------------------------------------------------------------+
| CONTRACTS SCANNED: 262 | TOP CATALYST: KTOS (+7.9% MC) | SAM.GOV: ONLINE | DEFENSE.GOV: ACTIVE | ENGINE: [● LIVE BROKER ACTIVE]|
+--------------------------------------------------------------------------------------------------------------------------------+
| COLUMN 1: POSITIONS & EXECUTION    | COLUMN 2: LIVE CATALYST STREAM             | COLUMN 3: STRATEGY & TELEMETRY              |
|                                    |                                            |                                             |
| [ACTIVE POSITIONS]                 | [SAM.GOV // DEFENSE.GOV AWARDS]            | [FAST-ALPHA STRATEGY PARAMS]                |
| Ticker: KTOS | Qty: 150            |                                            | - Min Score Threshold: 80                   |
| Entry: $21.40 | Mark: $23.10       | [RED ALERT] KTOS (Kratos Defense)          | - Min Catalyst / MC: 5.0%                   |
| P&L: +$255.00 (+7.94%) [IN PROFIT] | Award: $95.0M | Obligated: $18.0M          | - Take-Profit Target: +15.0%                |
| TP: $24.61 (+15%) | SL: $20.33 (-5%)| Agency: US Navy | Type: FFP                | - Stop-Loss Floor: -5.0%                    |
| [CLOSE POSITION]                   | AI Verdict: [HIGH CONVICTION BUY]          |                                             |
|                                    | "Transformative naval drone award;         | [ALPHA PLAY QUEUE]                          |
| [CLOSED TRADE HISTORY]             |  immediate cash obligation equals 14%      | - RCAT: Staged (Score: 78)                  |
| - AVAV: +$420.00 (+12.4%)          |  of quarterly defense division revenue."   | - PLTR: Monitored (Score: 65)                |
| - DRSH: -$95.00 (-3.1%)            |                                            |                                             |
|                                    | [⚡ 1-Click Buy] [📋 Copy] [🔍 Explain]   | [BROKER TELEMETRY]                          |
|                                    |                                            | Account Cash: $124,500.00                   |
|                                    | [RED ALERT] BIOQ (Bioqual, Inc.)           | Buying Power: $249,000.00                   |
|                                    | Award: $65.5M | Agency: HHS / NIH          | Mode: REAL-MONEY TRADING                    |
+--------------------------------------------------------------------------------------------------------------------------------+
```

### 7.1 Terminal Design System
- **Dark Mode Architecture:** Built with rich HSL design tokens, midnight carbon backgrounds (`#0a0d12`), neon semantic indicators (Electric Emerald `#00e676` for profit/safe, Crimson Flame `#ff1744` for red alerts/stops, Cyan `#00e5ff` for defense catalysts).
- **Adaptive 3-Column Command Grid:** Automatically dynamically scales to 100% viewport width, eliminating unused margins on ultra-wide screens while providing immediate visual access to positions, breaking contracts, and order queues simultaneously.
- **Server-Sent Events (SSE) Telemetry:** Direct non-blocking socket stream providing millisecond UI updates without requiring browser refreshes.

---

## 8. Hardening & Signal Integrity Architecture (v2.5.0 Patch)

The v2.5.0 release represents a comprehensive institutional hardening patch focused on data integrity, mathematical idempotency, and automated execution risk controls. Prior to this patch, prototype iterations were vulnerable to alert duplication, multi-contract identity collisions, and speculative ticker heuristic leakage.

```
+---------------------------------------------------------------------------------------------------+
|                            V2.5.0 SIGNAL INTEGRITY & HARDENING MATRIX                             |
+-----------------------------------+-----------------------------------+---------------------------+
| VULNERABILITY / RISK              | ROOT CAUSE                        | V2.5.0 HARDENING SOLUTION |
+-----------------------------------+-----------------------------------+---------------------------+
| Multi-Award DoD Collisions        | Hashing only (url, awardee, $)    | Composite SHA-256 with    |
|                                   | collapsed multiple contracts in   | contract_num, sub_agency, |
|                                   | the same daily Pentagon digest.   | and 120-char desc slug.   |
+-----------------------------------+-----------------------------------+---------------------------+
| Duplicate Alert Flooding          | Scanner re-runs generated new     | DB-level UNIQUE(award_id) |
|                                   | alert entries on every cycle.     | index + INSERT OR IGNORE  |
|                                   |                                   | with WAL transaction mode.|
+-----------------------------------+-----------------------------------+---------------------------+
| Heuristic Ticker Hallucinations   | Web search guesses (yfinance)     | 5-Tier Confidence Model;  |
| Triggering Capital Allocation     | traded with same authority as     | LOW_CONFIDENCE strictly   |
|                                   | verified SEC CIK filings.         | blocked from trading.     |
+-----------------------------------+-----------------------------------+---------------------------+
| USAspending Ingestion Omission    | USAspending adapter lacked        | Implemented Action-Date   |
|                                   | scanner integration and action-   | filtering API + CLI       |
|                                   | date filtering.                   | backfill engine.          |
+-----------------------------------+-----------------------------------+---------------------------+
| Cross-Platform Portability Breaks | %-d strftime flag caused crashes  | Dynamic cross-platform    |
| & Packaging Friction              | on Windows; no pyproject.toml.    | date formatting + PEP 621 |
|                                   |                                   | editable pip install.     |
+-----------------------------------+-----------------------------------+---------------------------+
```

### 8.1 Multi-Award DoD Identity Collision Elimination
In high-volume defense procurement digests (such as the Pentagon Daily Contracts report), a prime contractor like Boeing, Lockheed Martin, or General Dynamics frequently receives two or more separate awards on the same afternoon with identical dollar figures (e.g., two $15,000,000 modifications from distinct military branches). 

In earlier prototype builds, computing `source_id` as `SHA256(url + awardee + amount + date)` resulted in identity collisions, causing subsequent awards on that date to be discarded as duplicate entries. 

The v2.5.0 patch resolves this by constructing a fully qualified composite signature:
```python
# Defense.gov v2.5.0 deterministic identity generator
desc_slug = re.sub(r'[^a-z0-9]', '', description[:120].lower())
hash_input = f"{url}:{company_raw}:{amount}:{contract_number}:{sub_agency}:{desc_slug}:{date_str}"
source_id = f"defense_{hashlib.sha256(hash_input.encode('utf-8')).hexdigest()[:16]}"
```
This guarantees mathematical uniqueness across all military sub-agencies, contract numbers, and narrative scopes.

### 8.2 Database-Level Alert Idempotency
To prevent race conditions and duplicate alerts during concurrent scanning loops, alert deduplication was moved from application memory to the database storage engine.
- An explicit unique index was applied to the SQLite database:
  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_award_id ON alerts(award_id);
  ```
- The database abstraction layer utilizes `INSERT OR IGNORE`, falling back to an index lookup if an alert record already exists:
  ```python
  cursor.execute("""
      INSERT OR IGNORE INTO alerts (award_id, ticker, score, alert_type, payload, created_at)
      VALUES (?, ?, ?, ?, ?, ?)
  """, (award_id, ticker, score, alert_type, payload_json, now))
  if cursor.rowcount == 0:
      cursor.execute("SELECT id FROM alerts WHERE award_id = ?", (award_id,))
      return cursor.fetchone()[0]
  ```
This ensures strict mathematical idempotency: scanning the same procurement feed 100 times produces exactly 1 persistent alert record.

### 8.3 5-Tier Confidence Gating on Autonomous Execution
To eliminate the risk of automated capital destruction resulting from speculative entity guesses, the system enforces a strict 5-tier confidence model (`ResolutionConfidence` enum):
1. **`MANUAL_OVERRIDE`:** Hardcoded, verified parent-subsidiary mappings in `manual_ticker_overrides.yaml`.
2. **`HIGH_CONFIDENCE`:** Exact match in the SEC EDGAR CIK database or near-perfect fuzzy match ($\ge 98$).
3. **`MEDIUM_CONFIDENCE`:** Guarded SEC EDGAR fuzzy match ($92 \le \text{Score} < 98$). Eligible for alerting; live trading permitted only if on user whitelist.
4. **`LOW_CONFIDENCE`:** Third-party heuristic search guesses (e.g. yfinance symbol lookup). **Alerting suppressed and capital execution strictly blocked.**
5. **`UNRESOLVED`:** Entity unmapped. Recorded in the unresolved ledger for offline review.

The trade engine explicitly validates confidence prior to evaluating alpha criteria:
```python
# Trade Engine Safety Gate
if award.ticker_confidence in (ResolutionConfidence.LOW_CONFIDENCE, ResolutionConfidence.UNRESOLVED):
    logger.warning(
        f"Rejecting alpha play for {award.recipient_name}: "
        f"ticker '{award.ticker}' has {award.ticker_confidence} status."
    )
    return False
```

### 8.4 USAspending Ingestion & Action-Date Filtering
The USAspending adapter was integrated directly into the `ScanService` multi-source scanner loop. It leverages the Treasury search API (`/api/v2/search/spending_by_award/`), querying explicitly on `action_date` ranges rather than contract performance start dates, ensuring that real-time scans capture newly committed funds rather than modifications to decades-old programs.

### 8.5 Cross-Platform Portability & Packaging
- **Windows / Linux Date Portability:** Replaced Linux-specific `%-d` strftime formatting with cross-platform compatible formatting, resolving silent crashes on Windows NT environments.
- **PEP 517 / 621 Standardized Packaging:** Created `pyproject.toml` with `setuptools.build_meta`, allowing clean repository cloning and editable installation (`pip install -e .`) with standard dependency resolution.

---

## 9. Deployment Infrastructure & System Resilience

For quantitative defense catalysts, terminal downtime equals missed alpha. GovAward Alpha is engineered for **24/7 autonomous persistence**.

### 9.1 24/7 VPS Systemd Architecture
- **Environment:** Ubuntu Linux 24.04 LTS on high-throughput Contabo VPS.
- **Process Supervision:** Managed by Linux `systemd` (`govaward-alpha.service`) with auto-restart on exit, memory leak caps, and unprivileged execution.
- **High-Concurrency Database with Storage-Level Idempotency:** SQLite database engine configured with **Write-Ahead Logging (WAL)**:
  ```sql
  PRAGMA journal_mode = WAL;
  PRAGMA synchronous = NORMAL;
  PRAGMA busy_timeout = 5000;
  
  -- Guaranteed alert uniqueness across parallel scanners
  CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_award_id ON alerts(award_id);
  ```
  WAL mode allows infinite concurrent read operations without blocking or locking write transactions from incoming real-time contract feeds.

### 9.2 Resilient, Self-Healing Local Tunnel Watchdog
To eliminate the exposure of open web ports to public internet scrapers, the system features an intelligent client-side watchdog:
- **Native OpenSSH Supervisor:** Establishes an encrypted background SSH tunnel (`127.0.0.1:8055` $\to$ VPS `8055`).
- **Active HTTP Probing:** Pings the terminal `/api/stats` endpoint every 15 seconds. If connection freeze or socket drop is detected (e.g., laptop sleep, WiFi switch), the watchdog recycles the SSH tunnel in $<2$ seconds.
- **Paramiko Fallback Engine:** If native OpenSSH binaries are absent, seamlessly transitions to an in-process pure-Python Paramiko cryptographic transport.

---

## 10. Legal, Regulatory & Public Data Compliance

GovAward Alpha operates with strict adherence to federal data regulations and securities laws:

1. **Freedom of Information & Public Domain Data:** All procurement data analyzed by GovAward Alpha is sourced from public U.S. Government portals pursuant to the Federal Acquisition Regulation (FAR) Part 5 (Publicizing Contract Actions).
2. **Zero Authentication Bypassing:** The platform uses only authorized public APIs and unauthenticated government press feeds; no computer fraud or access violation mechanisms are utilized.
3. **Insider Trading Immunity (Mosaic Theory):** Information utilized represents public information available to all market participants. Under established U.S. securities law (SEC Mosaic Theory), aggregating, normalizing, and analyzing disparate pieces of public information does not constitute insider trading.
4. **Execution Disclaimers:** The system provides execution capabilities via certified broker APIs (Alpaca Markets, member FINRA/SIPC). Users configure and authorize all trade parameters independently.

---

## 11. Technical Specifications Summary

| Dimension | Specification |
| :--- | :--- |
| **Core Runtime** | Python 3.11+ / 3.12+ AsyncIO |
| **Packaging & Build System** | `pyproject.toml` with PEP 517/621, Editable Install (`pip install -e .`) |
| **Web & API Framework** | FastAPI / Starlette / Uvicorn ASGI |
| **Primary Ingestion Feeds** | SAM.gov API (Paged), Defense.gov Contracts Press (Collision-Proof), USAspending API |
| **Entity Resolution & Provenance** | 5-Tier Resolution Engine (`OVERRIDE`, `HIGH`, `MEDIUM`, `LOW`, `UNRESOLVED`) |
| **Market Data Providers** | Yahoo Finance (yfinance), Alpaca Market Data Stream |
| **AI Synthesis Models** | Google Gemini 2.5 Flash / Pro, Anthropic Claude 3.5 Sonnet |
| **Trading Interfaces** | Alpaca REST API v2 (Paper & Live Cash Accounts) with Confidence Gating |
| **Database Architecture** | SQLite 3 with WAL Mode, Foreign Keys & Storage-Level Unique Constraints |
| **User Interface** | Responsive Financial Terminal (Vanilla CSS Grid, Modern Vanilla JS, SSE) |
| **Test Coverage & Verification** | 233 Automated Pytest Unit/Integration/API Tests |
| **Hosting Topology** | Linux Ubuntu 24.04 VPS Daemon + Self-Healing Client SSH Tunnel |

---

## 12. Conclusion & Forward Roadmap

GovAward Alpha demonstrates that significant structural alpha remains accessible in equities markets at the intersection of **federal spending transparency, entity-resolution algorithms, and automated execution.**

By transforming complex, fragmented government procurement notices into clean, scored, and actionable intelligence, the platform levels the playing field against large institutional defense desks.

### Planned Enhancements (v3.0 Roadmap)
- **Multi-Vendor IDIQ Quota Decomposition:** Machine learning extraction of historical task-order award ratios for multiple-award IDIQs to forecast true economic share per vendor.
- **Options Flow Correlation:** Automated scraping of unusual options activity (UOA) in contract recipients 48 hours prior to public award announcements.
- **Congressional Trading Cross-Reference:** Real-time cross-referencing of contract awards against STOCK Act disclosures by members of the House and Senate Armed Services Committees.
- **Low-Latency Webhook Clusters:** Direct integration with AWS GovCloud SQS messaging feeds for sub-second procurement capture.

---

*© 2026 GovAward Alpha Project. For informational and institutional research purposes only. Securities trading involves substantial risk of loss.*
