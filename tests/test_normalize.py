"""Tests for amount parsing and company name normalization."""

import pytest

from govaward_alpha.normalize import (
    content_hash,
    detect_flags,
    extract_obligated_amount,
    normalize_company_name,
    parse_dollar_amount,
)


class TestParseDollarAmount:
    def test_plain_numeric(self):
        assert parse_dollar_amount("$95,000,000") == 95_000_000.0

    def test_million_suffix(self):
        assert parse_dollar_amount("$127.4 million") == pytest.approx(127_400_000.0)

    def test_billion_suffix(self):
        assert parse_dollar_amount("$1.2 billion") == pytest.approx(1_200_000_000.0)

    def test_maximum_prefix(self):
        assert parse_dollar_amount("maximum $95,000,000") == 95_000_000.0

    def test_not_to_exceed(self):
        assert parse_dollar_amount("not-to-exceed $75,000,000") == 75_000_000.0

    def test_obligated_phrase(self):
        text = "obligated at the time of award: $18,000,000"
        assert parse_dollar_amount(text) == 18_000_000.0

    def test_millions_abbrev_M(self):
        assert parse_dollar_amount("$50M") == pytest.approx(50_000_000.0)

    def test_billions_abbrev_B(self):
        assert parse_dollar_amount("$2B") == pytest.approx(2_000_000_000.0)

    def test_none_on_empty(self):
        assert parse_dollar_amount("") is None

    def test_none_on_no_match(self):
        assert parse_dollar_amount("no amount here") is None

    def test_comma_separated_billions(self):
        assert parse_dollar_amount("$1,200,000,000") == 1_200_000_000.0

    def test_thousand_suffix(self):
        assert parse_dollar_amount("$500K") == pytest.approx(500_000.0)


class TestExtractObligatedAmount:
    def test_obligated_phrase(self):
        text = (
            "KRATOS DEFENSE is being awarded a maximum $95,000,000 contract. "
            "Obligated at the time of award: $18,000,000."
        )
        assert extract_obligated_amount(text) == 18_000_000.0

    def test_funded_phrase(self):
        text = "Funded amount: $22,000,000 from Navy operations funds."
        assert extract_obligated_amount(text) == 22_000_000.0

    def test_returns_none_when_missing(self):
        text = "A contract for services worth $95,000,000 with no obligation detail."
        assert extract_obligated_amount(text) is None


class TestDetectFlags:
    def test_idiq_detected(self):
        flags = detect_flags("This is an IDIQ contract for IT services.")
        assert flags["is_idiq"] is True

    def test_indefinite_delivery(self):
        flags = detect_flags("Indefinite-delivery indefinite-quantity task order contract.")
        assert flags["is_idiq"] is True

    def test_modification_detected(self):
        flags = detect_flags("Modification No. P00042 to contract W911NF-22.")
        assert flags["is_modification"] is True

    def test_option_exercise_detected(self):
        flags = detect_flags("The Navy is exercising an option for additional services.")
        assert flags["is_option_exercise"] is True

    def test_ceiling_detected(self):
        flags = detect_flags("Not-to-exceed $50,000,000 ceiling value.")
        assert flags["is_ceiling_only"] is True

    def test_defense_keywords(self):
        flags = detect_flags("Radar surveillance drone reconnaissance ISR support.")
        assert flags["has_defense_keywords"] is True

    def test_clean_contract(self):
        flags = detect_flags("IT support services for federal agency.")
        assert flags["is_idiq"] is False
        assert flags["is_modification"] is False


class TestNormalizeCompanyName:
    def test_removes_legal_suffixes(self):
        # "solutions" and "inc" are stripped; "defense", "security" are meaningful and kept
        result = normalize_company_name("Kratos Defense & Security Solutions, Inc.")
        assert "kratos" in result
        assert "defense" in result
        assert "security" in result
        assert "inc" not in result.split()

    def test_lowercases(self):
        assert normalize_company_name("LOCKHEED MARTIN CORP") == "lockheed martin"

    def test_removes_punctuation(self):
        # "llc" and "," are stripped; "technologies" is a meaningful differentiator and kept
        result = normalize_company_name("L3Harris Technologies, LLC")
        assert "l3harris" in result
        assert "llc" not in result.split()

    def test_empty_string(self):
        assert normalize_company_name("") == ""

    def test_unicode_handled(self):
        result = normalize_company_name("Société Générale")
        assert result  # should not raise

    def test_strips_whitespace(self):
        assert normalize_company_name("  General Dynamics  ") == "general dynamics"


class TestContentHash:
    def test_deterministic(self):
        d = {"source": "sam_gov", "amount": 50_000_000, "awardee": "KTOS"}
        assert content_hash(d) == content_hash(d)

    def test_different_dicts_different_hashes(self):
        d1 = {"a": 1}
        d2 = {"a": 2}
        assert content_hash(d1) != content_hash(d2)

    def test_key_order_invariant(self):
        d1 = {"a": 1, "b": 2}
        d2 = {"b": 2, "a": 1}
        assert content_hash(d1) == content_hash(d2)
