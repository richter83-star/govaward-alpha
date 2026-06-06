"""Claude-powered award enrichment: resolver verification, contract quality, and materiality narrative."""

from __future__ import annotations

import json
import logging
from typing import Any

from govaward_alpha.config import get_settings
from govaward_alpha.models import Award, Company, ScoreResult

logger = logging.getLogger(__name__)


class ClaudeAnalysis:
    """Structured output from Claude enrichment."""

    __slots__ = (
        "resolver_confidence",
        "resolver_note",
        "contract_quality",
        "quality_note",
        "materiality_narrative",
        "watch_flags",
        "raw_response",
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
    ) -> None:
        self.resolver_confidence = resolver_confidence   # high | medium | low | unknown
        self.resolver_note = resolver_note
        self.contract_quality = contract_quality         # real_revenue | idiq_ceiling | modification | unclear
        self.quality_note = quality_note
        self.materiality_narrative = materiality_narrative
        self.watch_flags = watch_flags or []
        self.raw_response = raw_response

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolver_confidence": self.resolver_confidence,
            "resolver_note": self.resolver_note,
            "contract_quality": self.contract_quality,
            "quality_note": self.quality_note,
            "materiality_narrative": self.materiality_narrative,
            "watch_flags": self.watch_flags,
        }


_SYSTEM_PROMPT = """\
You are a U.S. government contract analyst helping a retail investor understand whether a federal award \
may be materially significant to a small or mid-cap public company. \
You have no access to real-time data — reason only from the information provided.

Respond ONLY with valid JSON matching this schema:
{
  "resolver_confidence": "high" | "medium" | "low",
  "resolver_note": "<one sentence on whether the ticker match looks correct>",
  "contract_quality": "real_revenue" | "idiq_ceiling" | "modification" | "unclear",
  "quality_note": "<one sentence on whether this award represents real booked revenue>",
  "materiality_narrative": "<2-3 sentences: investment context, what this award means for the company>",
  "watch_flags": ["<concern 1>", "<concern 2>"]
}

Rules:
- resolver_confidence "low" if the awardee name and ticker name share almost no words or the awardee appears to be a private company / nonprofit.
- contract_quality "idiq_ceiling" if description mentions IDIQ, ceiling, maximum, or task orders without guaranteed funding.
- contract_quality "modification" if this is a contract mod, option exercise, or price adjustment.
- contract_quality "real_revenue" if this is a firm-fixed-price new award or delivery order with immediate obligation.
- watch_flags: list up to 4 concrete concerns (e.g. "IDIQ ceiling not guaranteed", "awardee may be subsidiary not parent", "market cap data missing").
- Be concise. Do not add disclaimers. Do not hedge about being an AI.
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

    flags = []
    if award.is_idiq:
        flags.append("IDIQ")
    if award.is_ceiling_only:
        flags.append("ceiling only")
    if award.is_modification:
        flags.append("modification")
    if award.is_option_exercise:
        flags.append("option exercise")
    flags_str = ", ".join(flags) if flags else "none"

    desc = award.description[:600] if award.description else "not provided"

    return f"""\
Award details:
- Awardee name (raw):      {award.awardee_name_raw}
- Resolved ticker:         {ticker}
- Resolved company name:   {company_name}
- Award amount:            {fmt(award.award_amount)}
- Obligated amount:        {fmt(award.obligated_amount)}
- Company market cap:      {mktcap}
- Company revenue (TTM):   {revenue}
- Agency:                  {award.agency}
- Award type:              {award.award_type}
- Contract flags:          {flags_str}
- Materiality score:       {result.score}/100 ({result.level})
- Description excerpt:     {desc}

Scoring reasons:
{chr(10).join(f"  - {r}" for r in result.reasons)}

Evaluate: (1) Is the ticker match plausible? (2) Does this award represent real booked revenue? \
(3) Write a materiality narrative for a retail investor monitoring this ticker."""


def enrich(award: Award, company: Company | None, result: ScoreResult) -> ClaudeAnalysis | None:
    """
    Call Claude to enrich an alert with resolver verification, contract quality, and narrative.
    Returns None if Claude API is not configured or the call fails.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        logger.debug("ANTHROPIC_API_KEY not set — skipping Claude enrichment.")
        return None

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — run: pip install anthropic")
        return None

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    user_prompt = _build_user_prompt(award, company, result)

    try:
        message = client.messages.create(
            model=settings.claude_model,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = message.content[0].text.strip()
    except Exception as exc:
        logger.warning("Claude API call failed: %s", exc)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON block if Claude prefixed with prose
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start:end])
            except json.JSONDecodeError:
                logger.warning("Claude returned non-JSON response: %s", raw[:200])
                return ClaudeAnalysis(raw_response=raw)
        else:
            logger.warning("Claude returned non-JSON response: %s", raw[:200])
            return ClaudeAnalysis(raw_response=raw)

    return ClaudeAnalysis(
        resolver_confidence=data.get("resolver_confidence", "unknown"),
        resolver_note=data.get("resolver_note", ""),
        contract_quality=data.get("contract_quality", "unclear"),
        quality_note=data.get("quality_note", ""),
        materiality_narrative=data.get("materiality_narrative", ""),
        watch_flags=data.get("watch_flags", []),
        raw_response=raw,
    )
