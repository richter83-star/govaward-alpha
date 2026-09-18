"""Pydantic models for awards, companies, alerts, and scoring results."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from govaward_alpha.timeutil import utcnow


class ResolutionConfidence:
    """Explicit confidence tiers for company name → ticker resolution."""

    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"  # Verified human mapping in yaml
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"  # Exact SEC CIK match or very high-score fuzzy
    MEDIUM_CONFIDENCE = "MEDIUM_CONFIDENCE"  # Guarded SEC fuzzy match
    LOW_CONFIDENCE = "LOW_CONFIDENCE"    # Heuristic guess (e.g. yfinance search)
    UNRESOLVED = "UNRESOLVED"            # No ticker resolved


class Award(BaseModel):
    """Normalized award record, ready for DB persistence."""

    # Identity
    id: int | None = None
    source: str                               # "sam_gov" | "defense_daily" | "usaspending"
    source_award_id: str                      # unique ID within that source
    source_url: str = ""

    # Dates
    posted_date: date | None = None
    award_date: date | None = None

    # Awardee
    awardee_name_raw: str = ""
    awardee_name_normalized: str = ""
    parent_company_name: str = ""
    ticker: str | None = None
    ticker_confidence: str = ResolutionConfidence.UNRESOLVED

    # Contracting agency
    agency: str = ""
    sub_agency: str = ""

    # Classification
    naics: str = ""
    psc: str = ""
    award_type: str = ""
    contract_number: str = ""

    # Amounts
    award_amount: float | None = None
    obligated_amount: float | None = None

    # Flags
    is_idiq: bool = False
    is_modification: bool = False
    is_option_exercise: bool = False
    is_ceiling_only: bool = False
    is_legacy_contract: bool = False

    # Period of performance
    period_start: date | None = None
    period_end: date | None = None

    # Free text
    description: str = ""

    # Scoring / materiality (populated during enrichment)
    materiality_score: int | None = None
    alert_level: str | None = None

    # Dedup / audit
    content_hash: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _sync_confidence(self) -> Award:
        if self.ticker and self.ticker_confidence == ResolutionConfidence.UNRESOLVED:
            self.ticker_confidence = ResolutionConfidence.HIGH_CONFIDENCE
        elif not self.ticker:
            self.ticker_confidence = ResolutionConfidence.UNRESOLVED
        return self


class Company(BaseModel):
    """Market data snapshot for a resolved ticker."""

    ticker: str
    company_name: str = ""
    market_cap: float | None = None
    trailing_revenue: float | None = None
    avg_volume: float | None = None
    price: float | None = None
    sector: str = ""
    industry: str = ""
    exchange: str = ""
    last_market_data_refresh: datetime | None = None


class ScoreResult(BaseModel):
    """Output of the scoring engine for one award."""

    score: int                    # 0–100
    level: str                    # RED | ORANGE | YELLOW | WATCH | BELOW_THRESHOLD
    reasons: list[str] = Field(default_factory=list)
    breakdown: dict[str, Any] = Field(default_factory=dict)


class AlertRecord(BaseModel):
    """Persisted alert row."""

    id: int | None = None
    award_id: int
    ticker: str
    materiality_score: int
    alert_level: str
    reasons_json: str = "[]"
    sent_console: bool = False
    sent_email: bool = False
    sent_telegram: bool = False
    sent_discord: bool = False
    gemini_analysis_json: str = ""
    claude_analysis_json: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class SourceRun(BaseModel):
    """Audit row for each source scan execution."""

    id: int | None = None
    source: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    status: str = "running"      # running | success | error
    records_seen: int = 0
    records_inserted: int = 0
    records_updated: int = 0
    error_message: str = ""
