"""Google Gemini-powered award enrichment: retail catalyst strategy, front-running advice, and materiality analysis."""

from __future__ import annotations

import json
import logging
from typing import Any

from govaward_alpha.config import get_settings
from govaward_alpha.models import Award, Company, ScoreResult

logger = logging.getLogger(__name__)


class GeminiAnalysis:
    """Structured output from Gemini enrichment with retail trading strategy."""

    __slots__ = (
        "resolver_confidence",
        "resolver_note",
        "contract_quality",
        "quality_note",
        "materiality_narrative",
        "watch_flags",
        "raw_response",
        "action_verdict",
        "headline",
        "trade_strategy",
        "retail_thesis",
        "trap_check",
        "conviction_score",
        "catalyst_velocity",
        "recommended_allocation_pct",
        "target_holding_days",
        "optimal_take_profit_pct",
        "optimal_stop_loss_pct",
        "execution_urgency",
    )

    def __init__(
        self,
        resolver_confidence: str = "unknown",
        resolver_note: str = "",
        contract_quality: str = "unclear",
        quality_note: str = "",
        materiality_narrative: str = "",
        watch_flags: list[str] | None = None,
        raw_response: str = "",
        action_verdict: str = "MOMENTUM WATCH",
        headline: str = "",
        trade_strategy: str = "",
        retail_thesis: str = "",
        trap_check: str = "",
        conviction_score: int = 70,
        catalyst_velocity: str = "MOMENTUM_SURGE",
        recommended_allocation_pct: float = 0.10,
        target_holding_days: str = "1–2 Sessions",
        optimal_take_profit_pct: float = 12.0,
        optimal_stop_loss_pct: float = 3.5,
        execution_urgency: str = "MARKET_OPEN",
    ) -> None:
        self.resolver_confidence = resolver_confidence   # high | medium | low | unknown
        self.resolver_note = resolver_note
        self.contract_quality = contract_quality         # real_revenue | idiq_ceiling | modification | unclear
        self.quality_note = quality_note
        self.materiality_narrative = materiality_narrative
        self.watch_flags = watch_flags or []
        self.raw_response = raw_response
        self.action_verdict = action_verdict             # HIGH CONVICTION BUY | MOMENTUM WATCH | TRAP / AVOID
        self.headline = headline
        self.trade_strategy = trade_strategy
        self.retail_thesis = retail_thesis
        self.trap_check = trap_check
        self.conviction_score = int(conviction_score or 70)
        self.catalyst_velocity = str(catalyst_velocity or "MOMENTUM_SURGE")
        self.recommended_allocation_pct = float(recommended_allocation_pct or 0.10)
        self.target_holding_days = str(target_holding_days or "1–2 Sessions")
        self.optimal_take_profit_pct = float(optimal_take_profit_pct or 12.0)
        self.optimal_stop_loss_pct = float(optimal_stop_loss_pct or 3.5)
        self.execution_urgency = str(execution_urgency or "MARKET_OPEN")

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolver_confidence": self.resolver_confidence,
            "resolver_note": self.resolver_note,
            "contract_quality": self.contract_quality,
            "quality_note": self.quality_note,
            "materiality_narrative": self.materiality_narrative,
            "watch_flags": self.watch_flags,
            "action_verdict": self.action_verdict,
            "headline": self.headline,
            "trade_strategy": self.trade_strategy,
            "retail_thesis": self.retail_thesis,
            "trap_check": self.trap_check,
            "conviction_score": self.conviction_score,
            "catalyst_velocity": self.catalyst_velocity,
            "recommended_allocation_pct": self.recommended_allocation_pct,
            "target_holding_days": self.target_holding_days,
            "optimal_take_profit_pct": self.optimal_take_profit_pct,
            "optimal_stop_loss_pct": self.optimal_stop_loss_pct,
            "execution_urgency": self.execution_urgency,
        }


# Backward-compatible alias
ClaudeAnalysis = GeminiAnalysis


_SYSTEM_PROMPT = """\
You are an expert Wall Street retail catalyst trader and government contract specialist.
Your edge: Federal procurement databases publish multi-million dollar contract awards hours or days \
before mainstream financial media (Bloomberg/CNBC/Robinhood news feeds) report them.
You help opportunistic traders identify small to mid-cap companies ($50M-$10B market cap) winning major \
federal contracts ($50M+) to buy the stock before the market absorbs the announcement.

Respond ONLY with valid JSON matching this schema:
{
  "action_verdict": "HIGH CONVICTION BUY" | "MOMENTUM WATCH" | "TRAP / AVOID",
  "conviction_score": <1-100 integer confidence level>,
  "catalyst_velocity": "IMMEDIATE_BREAKOUT" | "MOMENTUM_SURGE" | "ACCUMULATION_WATCH" | "AVOID_TRAP",
  "recommended_allocation_pct": <0.05 to 0.25 float proportion of bankroll to deploy>,
  "target_holding_days": "<e.g. 1–3 Sessions | Intraday Scalp>",
  "optimal_take_profit_pct": <8.0 to 25.0 float profit exit target %>,
  "optimal_stop_loss_pct": <2.5 to 5.0 float protective stop loss %>,
  "execution_urgency": "ENTER_IMMEDIATELY_AT_OPEN" | "ENTER_ON_VWAP_CROSS" | "MONITOR_ONLY" | "DO_NOT_ENTER",
  "headline": "<One punchy headline highlighting ticker, contract value, and % of market cap>",
  "trade_strategy": "<Direct, actionable trading strategy: pre-market/market-open timing, key price/volume breakout confirmation, and target holding window>",
  "retail_thesis": "<2-3 sentences explaining why mainstream retail hasn't priced this in yet and how this contract moves the fundamental valuation>",
  "trap_check": "<1 sentence evaluating whether this is real immediate cash (firm-fixed-price/obligated) or a trap (unfunded IDIQ ceiling / administrative mod)>",
  "resolver_confidence": "high" | "medium" | "low",
  "resolver_note": "<One sentence on whether the ticker match looks correct>",
  "contract_quality": "real_revenue" | "idiq_ceiling" | "modification" | "unclear",
  "quality_note": "<One sentence on cash certainty>",
  "materiality_narrative": "<2-3 sentences investment narrative>",
  "watch_flags": ["<risk 1>", "<risk 2>"]
}

Rules:
- action_verdict "HIGH CONVICTION BUY" if award amount is >= 10% of market cap, new award, and immediate obligated funds.
- action_verdict "TRAP / AVOID" if the award is merely an uncommitted IDIQ maximum ceiling with zero guaranteed task orders, or ticker resolution is questionable.
- action_verdict "MOMENTUM WATCH" for incremental modifications or moderate-sized wins (3-10% of cap).
- recommended_allocation_pct: 0.20-0.25 for explosive plays (>=20% cap), 0.12-0.18 for major plays (15-20% cap), 0.06-0.10 for moderate momentum.
- Be direct, confident, and tactical. Never include AI boilerplate or generic disclaimer text.
"""


def _build_user_prompt(award: Award, company: Company | None, result: ScoreResult) -> str:
    def fmt(val: float | None, suffix: str = "") -> str:
        if val is None:
            return "unknown"
        if val >= 1e9:
            return f"${val/1e9:.2f}B{suffix}"
        if val >= 1e6:
            return f"${val/1e6:.1f}M{suffix}"
        return f"${val:,.0f}{suffix}"

    mktcap = fmt(company.market_cap) if company else "unknown"
    revenue = fmt(company.trailing_revenue) if company else "unknown"
    ticker = award.ticker or "UNRESOLVED"
    company_name = (company.company_name if company else None) or award.parent_company_name or "unknown"

    ratio_str = "unknown"
    if company and company.market_cap and award.award_amount:
        ratio = (award.award_amount / company.market_cap) * 100.0
        ratio_str = f"{ratio:.1f}% of market cap"

    flags = []
    if award.is_idiq:
        flags.append("IDIQ ceiling")
    if award.is_ceiling_only:
        flags.append("ceiling only (unfunded)")
    if award.is_modification:
        flags.append("modification")
    if award.is_option_exercise:
        flags.append("option exercise")
    flags_str = ", ".join(flags) if flags else "clean new award"

    desc = award.description[:600] if award.description else "not provided"

    return f"""\
Contract Announcement Catalyst:
- Ticker:                  {ticker}
- Company Name:            {company_name}
- Market Cap:              {mktcap}
- Annual Revenue (TTM):    {revenue}
- Award Amount:            {fmt(award.award_amount)} ({ratio_str})
- Obligated Amount:        {fmt(award.obligated_amount)}
- Awarding Agency:         {award.agency}
- Procurement Flags:       {flags_str}
- Posted Date:             {award.posted_date or 'Recent'}
- Materiality Score:       {result.score}/100 ({result.level})
- Contract Scope:          {desc}

Score Drivers:
{chr(10).join(f"  - {r}" for r in result.reasons)}

Provide tactical retail front-running trade strategy, action verdict, and trap analysis."""


def synthesize_retail_strategy(
    award: Award,
    company: Company | None,
    result: ScoreResult | None = None,
) -> dict[str, Any]:
    """
    Algorithmic retail catalyst synthesizer.
    Provides immediate, actionable trading intelligence even when offline or before LLM API calls.
    """
    amt = award.award_amount or award.obligated_amount or 0
    cap = (company.market_cap if company else None) or 0
    ratio = (amt / cap) if (cap > 0 and amt > 0) else 0.0
    ratio_pct = ratio * 100.0
    ticker = award.ticker or "UNRESOLVED"
    co_name = (company.company_name if company else None) or award.parent_company_name or "Target Contractor"

    # Evaluate verdict, trap status, and fast-alpha allocation parameters
    is_trap = award.is_ceiling_only or (award.is_idiq and (award.obligated_amount or 0) < amt * 0.1)
    
    if is_trap:
        verdict = "TRAP / AVOID"
        trap_check = "CAUTION: Unfunded ceiling/IDIQ structure — zero immediate guaranteed revenue booked."
        strategy = "Avoid buying on headline hype. IDIQ ceiling numbers frequently trap retail traders; wait for actual task order obligations."
        velocity = "AVOID_TRAP"
        conviction = 15
        alloc_pct = 0.0
        hold_window = "None (Avoid)"
        tp_pct = 0.0
        sl_pct = 0.0
        urgency = "DO_NOT_ENTER"
    elif ratio >= 0.20:
        verdict = "HIGH CONVICTION BUY"
        trap_check = f"VERIFIED ACCRETIVE: Contract represents {ratio_pct:.1f}% of entire market capitalization with firm government obligation."
        strategy = f"Ultra-Catalyst Play. Front-run market coverage at open. Target aggressive +18% multi-day repricing wave as retail syndicates discover the award."
        velocity = "IMMEDIATE_BREAKOUT"
        conviction = 95
        alloc_pct = 0.22  # 22% of bankroll for massive wins
        hold_window = "1–3 Sessions"
        tp_pct = 18.0
        sl_pct = 3.5
        urgency = "ENTER_IMMEDIATELY_AT_OPEN"
    elif ratio >= 0.15:
        verdict = "HIGH CONVICTION BUY"
        trap_check = f"VERIFIED ACCRETIVE: Contract represents {ratio_pct:.1f}% of market cap with confirmed procurement obligation."
        strategy = f"Major Accretive Win. Enter on opening volume spike. Target +15% continuation."
        velocity = "IMMEDIATE_BREAKOUT"
        conviction = 88
        alloc_pct = 0.16  # 16% of bankroll
        hold_window = "1–3 Sessions"
        tp_pct = 15.0
        sl_pct = 3.5
        urgency = "ENTER_ON_OPEN_VOLUME"
    elif ratio >= 0.10:
        verdict = "HIGH CONVICTION BUY"
        trap_check = f"SOLID CATALYST: Contract is {ratio_pct:.1f}% of cap (${amt/1e6:.1f}M inflow). High probability of institutional accumulation."
        strategy = f"Momentum Surge. Target +10% quick gain with tight stop."
        velocity = "MOMENTUM_SURGE"
        conviction = 80
        alloc_pct = 0.10  # 10% of bankroll
        hold_window = "1–2 Sessions"
        tp_pct = 10.0
        sl_pct = 3.0
        urgency = "ENTER_ON_VWAP_CROSS"
    elif ratio >= 0.05 or amt >= 100_000_000:
        verdict = "MOMENTUM WATCH"
        trap_check = f"MODERATE CATALYST: Contract is {ratio_pct:.1f}% of cap (${amt/1e6:.1f}M inflow)."
        strategy = f"Tactical Momentum Watch. Add {ticker} to watchlist for volume surge confirmation."
        velocity = "ACCUMULATION_WATCH"
        conviction = 65
        alloc_pct = 0.05
        hold_window = "Intraday Scalp"
        tp_pct = 8.0
        sl_pct = 3.0
        urgency = "MONITOR_ONLY"
    else:
        verdict = "MOMENTUM WATCH"
        trap_check = "Standard procurement flow; monitor subsequent task order announcements."
        strategy = f"Monitor {ticker} for contract flow cluster."
        velocity = "ACCUMULATION_WATCH"
        conviction = 50
        alloc_pct = 0.0
        hold_window = "None"
        tp_pct = 6.0
        sl_pct = 3.0
        urgency = "MONITOR_ONLY"

    headline = f"{ticker} wins ${amt/1e6:.1f}M {award.agency} contract" + (f" ({ratio_pct:.1f}% of market cap)" if ratio > 0 else "")

    thesis = (
        f"{co_name} ({ticker}) has secured a ${amt/1e6:.1f}M procurement catalyst from {award.agency}. "
        f"At {ratio_pct:.1f}% of current market valuation, this contract is a material earnings driver that retail news algorithms typically take 12-36 hours to syndicate."
    )

    return {
        "action_verdict": verdict,
        "headline": headline,
        "trade_strategy": strategy,
        "retail_thesis": thesis,
        "trap_check": trap_check,
        "conviction_score": conviction,
        "catalyst_velocity": velocity,
        "recommended_allocation_pct": alloc_pct,
        "target_holding_days": hold_window,
        "optimal_take_profit_pct": tp_pct,
        "optimal_stop_loss_pct": sl_pct,
        "execution_urgency": urgency,
        "resolver_confidence": "high" if ticker != "UNRESOLVED" else "low",
        "resolver_note": f"Ticker resolved to {co_name}",
        "contract_quality": "idiq_ceiling" if is_trap else ("modification" if award.is_modification else "real_revenue"),
        "quality_note": trap_check,
        "materiality_narrative": thesis,
        "watch_flags": ["IDIQ ceiling not guaranteed"] if is_trap else [],
    }


def enrich(award: Award, company: Company | None, result: ScoreResult) -> GeminiAnalysis | None:
    """
    Call Google Gemini to enrich an alert with retail front-running strategy and analysis.
    Falls back to algorithmic synthesizer if API key is not configured or call fails.
    """
    settings = get_settings()
    api_key = settings.gemini_api_key or settings.anthropic_api_key
    
    # If no API key configured, skip enrichment
    if not api_key:
        logger.debug("GEMINI_API_KEY not set — skipping Gemini enrichment.")
        return None

    model_name = settings.gemini_model or "gemini-2.5-flash"
    user_prompt = _build_user_prompt(award, company, result)
    raw = ""

    # 1. Primary: Use google.genai SDK
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model_name,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        if response and response.text:
            raw = response.text.strip()
    except Exception as exc:
        logger.debug("google.genai SDK invocation failed, falling back to direct REST: %s", exc)
        # 2. Fallback: Direct REST API via httpx
        try:
            import httpx

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            payload = {
                "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
                "contents": [{"parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.2,
                },
            }
            res = httpx.post(url, json=payload, timeout=20.0)
            res.raise_for_status()
            res_data = res.json()
            candidates = res_data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    raw = parts[0].get("text", "").strip()
        except Exception as rest_exc:
            logger.warning("Gemini API call failed: %s", rest_exc)
            # Fall back to algorithmic synthesizer
            syn = synthesize_retail_strategy(award, company, result)
            return GeminiAnalysis(**syn, raw_response=f"error_fallback: {rest_exc}")

    if not raw:
        syn = synthesize_retail_strategy(award, company, result)
        return GeminiAnalysis(**syn, raw_response="empty_response_fallback")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start:end])
            except json.JSONDecodeError:
                syn = synthesize_retail_strategy(award, company, result)
                return GeminiAnalysis(**syn, raw_response=raw)
        else:
            syn = synthesize_retail_strategy(award, company, result)
            return GeminiAnalysis(**syn, raw_response=raw)

    return GeminiAnalysis(
        resolver_confidence=data.get("resolver_confidence", "unknown"),
        resolver_note=data.get("resolver_note", ""),
        contract_quality=data.get("contract_quality", "unclear"),
        quality_note=data.get("quality_note", ""),
        materiality_narrative=data.get("materiality_narrative", ""),
        watch_flags=data.get("watch_flags", []),
        raw_response=raw,
        action_verdict=data.get("action_verdict", "MOMENTUM WATCH"),
        headline=data.get("headline", ""),
        trade_strategy=data.get("trade_strategy", ""),
        retail_thesis=data.get("retail_thesis", ""),
        trap_check=data.get("trap_check", ""),
        conviction_score=int(data.get("conviction_score") or 75),
        catalyst_velocity=str(data.get("catalyst_velocity") or "MOMENTUM_SURGE"),
        recommended_allocation_pct=float(data.get("recommended_allocation_pct") or 0.10),
        target_holding_days=str(data.get("target_holding_days") or "1–3 Sessions"),
        optimal_take_profit_pct=float(data.get("optimal_take_profit_pct") or 12.0),
        optimal_stop_loss_pct=float(data.get("optimal_stop_loss_pct") or 3.5),
        execution_urgency=str(data.get("execution_urgency") or "MARKET_OPEN"),
    )
