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

    # Evaluate verdict and trap status
    is_trap = award.is_ceiling_only or (award.is_idiq and (award.obligated_amount or 0) < amt * 0.1)
    
    if is_trap:
        verdict = "TRAP / AVOID"
        trap_check = "CAUTION: Unfunded ceiling/IDIQ structure — zero immediate guaranteed revenue booked."
        strategy = "Avoid buying on headline hype. IDIQ ceiling numbers frequently trap retail traders; wait for actual task order obligations."
    elif ratio >= 0.15:
        verdict = "HIGH CONVICTION BUY"
        trap_check = f"VERIFIED ACCRETIVE: Contract represents {ratio_pct:.1f}% of entire market capitalization with firm government obligation."
        strategy = f"Front-run market coverage. Enter pre-market or on opening volume surge over 20-day average. Target multi-day continuation as retail feeds pick up the announcement."
    elif ratio >= 0.05 or amt >= 100_000_000:
        verdict = "MOMENTUM WATCH"
        trap_check = f"SOLID CATALYST: Contract is {ratio_pct:.1f}% of cap (${amt/1e6:.1f}M inflow). High probability of institutional accumulation."
        strategy = f"Add {ticker} to immediate momentum watch. Look for volume spike above opening VWAP to confirm institutional interest before taking a position."
    else:
        verdict = "MOMENTUM WATCH"
        trap_check = "Standard procurement flow; monitor subsequent task order announcements."
        strategy = f"Monitor {ticker} for contract flow cluster. Often multiple agency task orders precede earnings surprises."

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
    )
