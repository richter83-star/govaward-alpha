"""Unit tests for Google Gemini enricher and backfill service."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from govaward_alpha import config, db
from govaward_alpha.enrich.gemini_enricher import (
    ClaudeAnalysis,
    GeminiAnalysis,
    enrich,
)
from govaward_alpha.models import Award, Company, ScoreResult
from govaward_alpha.services.enrich_service import backfill_gemini_analysis


def _sample_award() -> Award:
    return Award(
        source="defense_daily",
        source_award_id="TEST-AWD-001",
        awardee_name_raw="Kratos Technology & Training Solutions, Inc.",
        parent_company_name="Kratos Defense & Security Solutions, Inc.",
        ticker="KTOS",
        award_amount=75_000_000.0,
        obligated_amount=75_000_000.0,
        agency="Department of the Air Force",
        posted_date=date(2026, 9, 1),
        award_date=date(2026, 9, 1),
        description="Delivery order for target drone maintenance and logistics support.",
    )


def _sample_company() -> Company:
    return Company(
        ticker="KTOS",
        company_name="Kratos Defense & Security Solutions, Inc.",
        market_cap=2_500_000_000.0,
        trailing_revenue=1_100_000_000.0,
        sector="Industrials",
        industry="Aerospace & Defense",
    )


def _sample_score() -> ScoreResult:
    return ScoreResult(
        score=82,
        level="RED",
        reasons=["Award represents 3.0% of market cap", "Fully obligated new award"],
        breakdown={},
    )


class TestGeminiAnalysisModel:
    def test_class_instantiation_and_slots(self):
        analysis = GeminiAnalysis(
            resolver_confidence="high",
            resolver_note="Plausible subsidiary match",
            contract_quality="real_revenue",
            quality_note="Immediate obligated funding",
            materiality_narrative="Material multi-year target drone contract.",
            watch_flags=["Option years not yet funded"],
            raw_response="{}",
        )
        assert analysis.resolver_confidence == "high"
        assert analysis.contract_quality == "real_revenue"
        d = analysis.to_dict()
        assert d["resolver_confidence"] == "high"
        assert d["contract_quality"] == "real_revenue"
        assert d["watch_flags"] == ["Option years not yet funded"]

    def test_claude_analysis_alias(self):
        assert ClaudeAnalysis is GeminiAnalysis


class TestGeminiEnrich:
    def test_enrich_returns_none_when_no_api_key(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        config._settings = None
        res = enrich(_sample_award(), _sample_company(), _sample_score())
        assert res is None

    def test_enrich_success_with_genai_sdk(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        config._settings = None

        mock_payload = {
            "resolver_confidence": "high",
            "resolver_note": "Direct match for KTOS subsidiary",
            "contract_quality": "real_revenue",
            "quality_note": "Immediate obligation",
            "materiality_narrative": "Highly accretive contract for Kratos.",
            "watch_flags": [],
        }

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = json.dumps(mock_payload)
        mock_client.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client):
            res = enrich(_sample_award(), _sample_company(), _sample_score())

        assert res is not None
        assert res.resolver_confidence == "high"
        assert res.contract_quality == "real_revenue"
        assert "accretive" in res.materiality_narrative

    def test_enrich_extracts_json_from_markdown_fences(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        config._settings = None

        raw_with_fences = """```json
        {
          "resolver_confidence": "medium",
          "resolver_note": "Subsidiary name match",
          "contract_quality": "modification",
          "quality_note": "Option exercise",
          "materiality_narrative": "Expansion of existing program.",
          "watch_flags": ["Scope modification"]
        }
        ```"""

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = raw_with_fences
        mock_client.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client):
            res = enrich(_sample_award(), _sample_company(), _sample_score())

        assert res is not None
        assert res.resolver_confidence == "medium"
        assert res.contract_quality == "modification"


class TestBackfillService:
    def test_backfill_skipped_without_key(self, monkeypatch, db_conn):
        monkeypatch.setenv("GEMINI_API_KEY", "")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        config._settings = None

        stats = backfill_gemini_analysis()
        assert stats.skipped_no_key is True
        assert stats.candidates == 0
