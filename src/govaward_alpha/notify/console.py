"""Rich console alert formatter."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from govaward_alpha.models import AlertRecord, Award, Company, ScoreResult

if TYPE_CHECKING:
    from govaward_alpha.enrich.gemini_enricher import GeminiAnalysis

console = Console()

_LEVEL_COLORS = {
    "RED": "bold red",
    "ORANGE": "bold dark_orange",
    "YELLOW": "bold yellow",
    "WATCH": "bold cyan",
}

_CONFIDENCE_LABELS = {
    "high": "HIGH",
    "medium": "MED",
    "low": "LOW ⚠",
    "unknown": "?",
}

_QUALITY_LABELS = {
    "real_revenue": "Real revenue",
    "idiq_ceiling": "IDIQ ceiling (not guaranteed)",
    "modification": "Modification / option",
    "unclear": "Unclear",
}


def _fmt_usd(val: float | None) -> str:
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
    if num is None or denom is None or denom == 0:
        return "N/A"
    return f"{num / denom * 100:.1f}%"


def render_alert(
    award: Award,
    company: Company | None,
    result: ScoreResult,
    gemini: "GeminiAnalysis | None" = None,
    claude: Any = None,
) -> str:
    """Render the canonical alert text (used by all channels)."""
    ai = gemini or claude
    lines = [
        f"{result.level} ALERT: Possible Material Federal Award",
        "",
        f"Ticker:          {award.ticker or 'UNRESOLVED'}",
        f"Company:         {award.parent_company_name or 'Unknown'}",
        f"Awardee:         {award.awardee_name_raw}",
        f"Award Amount:    {_fmt_usd(award.award_amount)}",
        f"Obligated Now:   {_fmt_usd(award.obligated_amount)}",
        f"Market Cap:      {_fmt_usd(company.market_cap if company else None)}",
        f"Award / Mkt Cap: {_fmt_pct(award.award_amount, company.market_cap if company else None)}",
        f"Oblg / Mkt Cap:  {_fmt_pct(award.obligated_amount, company.market_cap if company else None)}",
        f"Award / Revenue: {_fmt_pct(award.award_amount, company.trailing_revenue if company else None)}",
        f"Agency:          {award.agency}" + (f" / {award.sub_agency}" if award.sub_agency else ""),
        f"Type:            {_type_flags(award)}",
        f"Posted Date:     {award.posted_date or 'N/A'}",
        f"Award Date:      {award.award_date or 'N/A'}",
        f"Source:          {award.source}",
        f"URL:             {award.source_url or 'N/A'}",
        "",
        f"Score:           {result.score} / 100",
        f"Level:           {result.level}",
        "",
        "Reasons:",
    ]

    for reason in result.reasons:
        lines.append(f"  - {reason}")

    if ai is not None:
        lines += [
            "",
            "─" * 60,
            "Gemini Analysis",
            "─" * 60,
            f"Resolver match:  {_CONFIDENCE_LABELS.get(ai.resolver_confidence, '?')}  {ai.resolver_note}",
            f"Contract type:   {_QUALITY_LABELS.get(ai.contract_quality, ai.contract_quality)}",
            f"                 {ai.quality_note}",
        ]

        if ai.materiality_narrative:
            lines += ["", "Investment context:"]
            for sentence in ai.materiality_narrative.split(". "):
                s = sentence.strip().rstrip(".")
                if s:
                    lines.append(f"  {s}.")

        if ai.watch_flags:
            lines += ["", "Watch flags:"]
            for flag in ai.watch_flags:
                lines.append(f"  ⚠  {flag}")

    lines += [
        "",
        "─" * 60,
        "Caution:",
        "  This alert is public-information research, not a trade instruction.",
        "  Verify contract economics, obligation timing, liquidity, SEC filings,",
        "  and company disclosure before acting.",
    ]

    return "\n".join(lines)


def _type_flags(award: Award) -> str:
    parts = []
    if award.award_type:
        parts.append(award.award_type)
    if award.is_idiq:
        parts.append("IDIQ")
    if award.is_modification:
        parts.append("Modification")
    if award.is_option_exercise:
        parts.append("Option Exercise")
    if award.is_ceiling_only:
        parts.append("Ceiling Only")
    return " / ".join(parts) if parts else "Award"


def print_alert(
    award: Award,
    company: Company | None,
    result: ScoreResult,
    gemini: "GeminiAnalysis | None" = None,
    claude: Any = None,
) -> None:
    """Print a formatted alert panel to the console."""
    text_body = render_alert(award, company, result, gemini or claude)
    level = result.level
    color = _LEVEL_COLORS.get(level, "white")

    panel = Panel(
        Text(text_body),
        title=f"[{color}] {level} ALERT [/{color}]",
        border_style=color,
        padding=(1, 2),
    )
    console.print(panel)
