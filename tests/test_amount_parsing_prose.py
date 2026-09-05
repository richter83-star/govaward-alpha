"""Regression tests for amount parsing inside prose.

Before the anchoring fix, ``parse_dollar_amount`` took the first number-like
token anywhere in the string. That read "5-year contract valued at
$95,000,000" as $5 and a contract number as a dollar figure — and because
sam_gov falls back to parsing the description when no structured amount is
present, a real $95M award could be filtered out as "below threshold".
"""

from __future__ import annotations

import pytest

from govaward_alpha.normalize import parse_dollar_amount


class TestProseAmountsAreAnchored:
    """An amount inside prose must carry a "$" or an explicit unit word."""

    def test_leading_ordinal_does_not_win_over_dollar_amount(self):
        text = "5-year contract valued at $95,000,000"
        assert parse_dollar_amount(text) == 95_000_000.0

    def test_multi_year_phrase_with_unit_amount(self):
        text = "a 3-year deal with a $1.2 billion ceiling"
        assert parse_dollar_amount(text) == pytest.approx(1_200_000_000.0)

    def test_contract_number_is_not_an_amount(self):
        text = "Contract W91234-26-C-0001 for 2000 units of widget"
        assert parse_dollar_amount(text) is None

    def test_naics_code_is_not_an_amount(self):
        text = "Firm-fixed-price contract, NAICS 541330, value TBD"
        assert parse_dollar_amount(text) is None

    def test_modification_number_is_not_an_amount(self):
        text = "P00012 mod for 24 items"
        assert parse_dollar_amount(text) is None

    def test_unit_word_alone_anchors_without_dollar_sign(self):
        assert parse_dollar_amount("75 million") == pytest.approx(75_000_000.0)

    def test_quantity_without_unit_or_dollar_is_ignored(self):
        assert parse_dollar_amount("delivery of 2000 units") is None


class TestStructuredFieldValuesStillParse:
    """Bare numerics are trusted when they are the whole input (API fields)."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("95000000", 95_000_000.0),
            ("95,000,000", 95_000_000.0),
            ("95000000.00", 95_000_000.0),
            ("$95,000,000", 95_000_000.0),
            ("  95000000  ", 95_000_000.0),
        ],
    )
    def test_bare_numeric_field(self, raw, expected):
        assert parse_dollar_amount(raw) == pytest.approx(expected)


class TestSamGovDescriptionFallback:
    """The end of the silent-drop bug, at the call site that caused it."""

    def test_award_survives_when_description_has_no_parseable_amount(self):
        from govaward_alpha.sources.sam_gov import _parse_opportunity

        opp = {
            "noticeId": "SMOKE-NO-AMOUNT",
            "awardee": {"name": "Acme Defense Corp"},
            "description": "Contract W91234-26-C-0001 for 2000 units of widget",
        }
        award = _parse_opportunity(opp)
        # Previously: description parsed as $91,234 -> below the $50M floor ->
        # award silently discarded. Now the amount is unknown, and an award
        # with an unknown amount is kept for downstream enrichment.
        assert award is not None
        assert award.award_amount is None

    def test_real_amount_in_prose_is_recovered(self):
        from govaward_alpha.sources.sam_gov import _parse_opportunity

        opp = {
            "noticeId": "SMOKE-PROSE-AMOUNT",
            "awardee": {"name": "Acme Defense Corp"},
            "description": "5-year contract valued at $95,000,000 for drone systems.",
        }
        award = _parse_opportunity(opp)
        assert award is not None
        assert award.award_amount == 95_000_000.0
