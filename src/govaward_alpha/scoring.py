"""Materiality scoring engine (0–100)."""

from __future__ import annotations

import logging
from typing import Any

from govaward_alpha.models import Award, Company, ScoreResult

logger = logging.getLogger(__name__)

LEVEL_RED = "RED"
LEVEL_ORANGE = "ORANGE"
LEVEL_YELLOW = "YELLOW"
LEVEL_WATCH = "WATCH"
LEVEL_BELOW = "BELOW_THRESHOLD"


def score_award(award: Award, company: Company | None) -> ScoreResult:
    """
    Compute a materiality score for an award.

    Scoring rubric (sum cannot exceed 100 before caps):
      Base ratios (award_amount vs market_cap, obligated vs market_cap,
                   award_amount vs revenue)
      Bonuses for small-cap, new award, low volume, defense keywords
      Penalties for IDIQ ceiling-only, modifications, mega-cap

    Returns ScoreResult with score, level, reasons, and breakdown.
    """
    reasons: list[str] = []
    breakdown: dict[str, Any] = {}
    score = 0

    award_amount = award.award_amount
    obligated = award.obligated_amount
    market_cap = company.market_cap if company else None
    revenue = company.trailing_revenue if company else None
    avg_vol = company.avg_volume if company else None

    # --- Award/MarketCap ratio -------------------------------------------
    if award_amount and market_cap and market_cap > 0:
        ratio = award_amount / market_cap
        breakdown["award_to_mktcap"] = round(ratio * 100, 2)
        if ratio >= 0.50:
            pts = 40
            reasons.append(f"Award is {ratio*100:.1f}% of market cap (≥50%) — very significant.")
        elif ratio >= 0.25:
            pts = 32
            reasons.append(f"Award is {ratio*100:.1f}% of market cap (≥25%).")
        elif ratio >= 0.10:
            pts = 22
            reasons.append(f"Award is {ratio*100:.1f}% of market cap (≥10%).")
        elif ratio >= 0.05:
            pts = 12
            reasons.append(f"Award is {ratio*100:.1f}% of market cap (≥5%).")
        else:
            pts = 5
        score += pts
        breakdown["pts_award_to_mktcap"] = pts
    else:
        breakdown["pts_award_to_mktcap"] = 0

    # --- Obligated/MarketCap ratio -----------------------------------------
    if obligated and market_cap and market_cap > 0:
        ratio = obligated / market_cap
        breakdown["obligated_to_mktcap"] = round(ratio * 100, 2)
        if ratio >= 0.10:
            pts = 25
            reasons.append(f"Obligated amount is {ratio*100:.1f}% of market cap (≥10%).")
        elif ratio >= 0.05:
            pts = 18
            reasons.append(f"Obligated amount is {ratio*100:.1f}% of market cap (≥5%).")
        elif ratio >= 0.02:
            pts = 10
            reasons.append(f"Obligated amount is {ratio*100:.1f}% of market cap (≥2%).")
        else:
            pts = 0
        score += pts
        breakdown["pts_obligated_to_mktcap"] = pts
    else:
        breakdown["pts_obligated_to_mktcap"] = 0

    # --- Award/Revenue ratio -----------------------------------------------
    if award_amount and revenue and revenue > 0:
        ratio = award_amount / revenue
        breakdown["award_to_revenue"] = round(ratio * 100, 2)
        if ratio >= 0.50:
            pts = 25
            reasons.append(f"Award is {ratio*100:.1f}% of trailing revenue (≥50%).")
        elif ratio >= 0.25:
            pts = 18
            reasons.append(f"Award is {ratio*100:.1f}% of trailing revenue (≥25%).")
        elif ratio >= 0.10:
            pts = 10
            reasons.append(f"Award is {ratio*100:.1f}% of trailing revenue (≥10%).")
        else:
            pts = 0
        score += pts
        breakdown["pts_award_to_revenue"] = pts
    else:
        breakdown["pts_award_to_revenue"] = 0

    # --- Bonuses -----------------------------------------------------------

    # Small/micro-cap bonus
    if market_cap and market_cap < 2_000_000_000:
        score += 10
        breakdown["bonus_small_cap"] = 10
        reasons.append("Company is small/micro-cap (< $2B market cap).")
    else:
        breakdown["bonus_small_cap"] = 0

    # New award (not modification)
    if not award.is_modification and not award.is_option_exercise:
        score += 10
        breakdown["bonus_new_award"] = 10
        reasons.append("New award, not a modification or option exercise.")
    else:
        breakdown["bonus_new_award"] = 0

    # Low average volume proxy
    if avg_vol and avg_vol < 1_000_000:
        score += 5
        breakdown["bonus_low_volume"] = 5
        reasons.append("Low average trading volume — potentially underfollowed.")
    else:
        breakdown["bonus_low_volume"] = 0

    # Defense/AI/cyber keywords
    from govaward_alpha.normalize import _DEFENSE_KEYWORDS
    desc_text = f"{award.description} {award.agency} {award.sub_agency} {award.naics} {award.psc}"
    if _DEFENSE_KEYWORDS.search(desc_text):
        score += 5
        breakdown["bonus_defense_keywords"] = 5
        reasons.append("Defense, AI, cyber, or space contract category detected.")
    else:
        breakdown["bonus_defense_keywords"] = 0

    # Placeholder: no recent press release found
    breakdown["bonus_no_pr"] = 0  # reserved for future enrichment

    # --- Penalties ---------------------------------------------------------

    # IDIQ ceiling with low/no obligation
    if award.is_idiq and (not obligated or (award_amount and obligated and obligated < 0.02 * award_amount)):
        penalty = 20
        score -= penalty
        breakdown["penalty_idiq_low_obligated"] = -penalty
        reasons.append("IDIQ ceiling with low or zero obligated amount at award — ceiling ≠ revenue.")
    else:
        breakdown["penalty_idiq_low_obligated"] = 0

    # Modification only
    if award.is_modification:
        penalty = 10
        score -= penalty
        breakdown["penalty_modification"] = -penalty
        reasons.append("Award is a modification — incremental, not a new contract.")
    else:
        breakdown["penalty_modification"] = 0

    # Clamp
    score = max(0, min(score, 100))
    breakdown["final_score"] = score

    # --- Level assignment --------------------------------------------------
    level = _assign_level(score)

    return ScoreResult(score=score, level=level, reasons=reasons, breakdown=breakdown)


def _assign_level(score: int) -> str:
    if score >= 80:
        return LEVEL_RED
    if score >= 65:
        return LEVEL_ORANGE
    if score >= 50:
        return LEVEL_YELLOW
    return LEVEL_WATCH
