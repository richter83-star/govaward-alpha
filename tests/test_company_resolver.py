"""Tests for company name resolver."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from govaward_alpha.enrich.company_resolver import resolve
from govaward_alpha.normalize import normalize_company_name


class TestResolveManualOverrides:
    """Tests using the built-in manual_ticker_overrides.yaml entries."""

    def test_resolves_palantir_usg(self):
        # "palantir usg inc" is in overrides
        result = resolve("Palantir USG Inc")
        assert result["ticker"] == "PLTR"
        assert "manual_override" in result["resolution_method"]

    def test_resolves_kratos(self):
        result = resolve("Kratos Defense & Security Solutions, Inc.")
        assert result["ticker"] == "KTOS"

    def test_resolves_booz_allen(self):
        result = resolve("Booz Allen Hamilton")
        assert result["ticker"] == "BAH"

    def test_empty_name_returns_none(self):
        result = resolve("")
        assert result["ticker"] is None
        assert result["resolution_method"] == "empty"


class TestResolveSecTickers:
    """Tests against SEC ticker map (mocked to avoid network calls)."""

    MOCK_TICKER_MAP = {
        "acme":         {"ticker": "ACME", "company_name": "Acme Corp", "cik": "123"},
        "lockheed martin": {"ticker": "LMT", "company_name": "Lockheed Martin Corp", "cik": "456"},
    }

    @patch("govaward_alpha.enrich.company_resolver.get_ticker_map", return_value=MOCK_TICKER_MAP)
    @patch("govaward_alpha.enrich.company_resolver.get_overrides", return_value={})
    def test_sec_exact_match(self, mock_overrides, mock_map):
        # "Acme Inc." normalizes to "acme" — exact key in mock map
        result = resolve("Acme Inc.")
        assert result["ticker"] == "ACME"
        assert result["resolution_method"] == "sec_exact"

    @patch("govaward_alpha.enrich.company_resolver.get_ticker_map", return_value=MOCK_TICKER_MAP)
    @patch("govaward_alpha.enrich.company_resolver.get_overrides", return_value={})
    def test_sec_fuzzy_match(self, mock_overrides, mock_map):
        # Slightly different name should fuzzy match
        result = resolve("Lockheed Martin Corporation")
        assert result["ticker"] == "LMT"

    @patch("govaward_alpha.enrich.company_resolver.get_ticker_map", return_value={})
    @patch("govaward_alpha.enrich.company_resolver.get_overrides", return_value={})
    @patch("govaward_alpha.enrich.company_resolver._guess_ticker_yfinance", return_value=None)
    def test_unresolved_returns_none(self, mock_yf, mock_overrides, mock_map):
        result = resolve("Completely Unknown Private Entity LLC")
        assert result["ticker"] is None
        assert result["resolution_method"] == "unresolved"


class TestFuzzyMatchGuards:
    """Regression: a shared generic word must not be enough to claim a ticker.

    normalize_company_name() strips "global"/"ltd" as legal-suffix noise, so
    SEC's "GLOBAL TECHNOLOGIES LTD" collapses to the bare key "technologies".
    WRatio's partial-ratio term then scored "ls technologies" against it at 95
    -- above the 92 threshold -- and a live scan mapped the private FAA
    contractor LS TECHNOLOGIES LLC onto the penny stock GTLL, firing a RED
    alert claiming the award was 4,690% of market cap.
    """

    GENERIC_MAP = {
        "technologies":    {"ticker": "GTLL", "company_name": "GLOBAL TECHNOLOGIES LTD", "cik": "1"},
        "lockheed martin": {"ticker": "LMT", "company_name": "Lockheed Martin Corp", "cik": "2"},
        "kratos defense security": {"ticker": "KTOS", "company_name": "Kratos Defense", "cik": "3"},
    }

    @patch("govaward_alpha.enrich.company_resolver.get_ticker_map", return_value=GENERIC_MAP)
    @patch("govaward_alpha.enrich.company_resolver.get_overrides", return_value={})
    @patch("govaward_alpha.enrich.company_resolver._guess_ticker_yfinance", return_value=None)
    def test_shared_generic_word_does_not_resolve(self, mock_yf, mock_overrides, mock_map):
        result = resolve("LS TECHNOLOGIES LLC")
        assert result["ticker"] is None
        assert result["resolution_method"] == "unresolved"

    @patch("govaward_alpha.enrich.company_resolver.get_ticker_map", return_value=GENERIC_MAP)
    @patch("govaward_alpha.enrich.company_resolver.get_overrides", return_value={})
    @patch("govaward_alpha.enrich.company_resolver._guess_ticker_yfinance", return_value=None)
    def test_matching_head_token_still_resolves(self, mock_yf, mock_overrides, mock_map):
        """The guard must not block genuine near-matches."""
        result = resolve("Lockheed Martin Corporation")
        assert result["ticker"] == "LMT"

    @patch("govaward_alpha.enrich.company_resolver.get_ticker_map", return_value=GENERIC_MAP)
    @patch("govaward_alpha.enrich.company_resolver.get_overrides", return_value={})
    @patch("govaward_alpha.enrich.company_resolver._guess_ticker_yfinance", return_value=None)
    def test_suffix_variation_still_resolves(self, mock_yf, mock_overrides, mock_map):
        result = resolve("Kratos Defense & Security Solutions, Inc.")
        assert result["ticker"] == "KTOS"


class TestNormalizeForResolution:
    """Confirm normalization used in resolution keys."""

    def test_lowercase(self):
        assert normalize_company_name("ACME CORP") == "acme"

    def test_removes_inc(self):
        norm = normalize_company_name("Smith Defense Inc.")
        assert "inc" not in norm

    def test_removes_llc(self):
        norm = normalize_company_name("TechSec LLC")
        assert "llc" not in norm

    def test_strips_and_group(self):
        norm = normalize_company_name("Northrop Grumman Group")
        assert "group" not in norm

    def test_ampersand_handled(self):
        # Ampersand becomes a space
        norm = normalize_company_name("Smith & Wesson")
        assert "smith" in norm
        assert "wesson" in norm


class TestAddOverride:
    """Test the add_override function writes correctly."""

    def test_add_override_roundtrip(self, tmp_path):
        import yaml
        from unittest.mock import patch as _patch

        override_file = tmp_path / "manual_ticker_overrides.yaml"
        override_file.write_text("overrides: {}\n", encoding="utf-8")

        mock_settings = MagicMock()
        mock_settings.data_dir = tmp_path

        with _patch("govaward_alpha.enrich.company_resolver.get_settings", return_value=mock_settings):
            with _patch("govaward_alpha.enrich.company_resolver._override_cache", None):
                from govaward_alpha.enrich.company_resolver import add_override
                add_override("Fictional Corp LLC", "FICT", "Fictional Corporation")

        data = yaml.safe_load(override_file.read_text(encoding="utf-8"))
        # add_override stores the raw name lowercased; key contains "fictional corp llc"
        keys = list(data["overrides"].keys())
        assert any("fictional" in k for k in keys)
        matched = next(k for k in keys if "fictional" in k)
        assert data["overrides"][matched]["ticker"] == "FICT"
