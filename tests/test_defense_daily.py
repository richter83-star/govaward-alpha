"""Tests for the Defense.gov daily-contracts parser.

These exercise the parsing layer directly with fixture HTML. The adapter
fetches over urllib (not httpx) because Defense.gov rejects the default TLS
fingerprint, so ``pytest-httpx`` cannot intercept it — parser-level tests are
the useful unit here, and they need no network at all.
"""

from __future__ import annotations

from datetime import date

import pytest

from govaward_alpha.sources.defense_daily import (
    _AWARDEE_PATTERN,
    _CONTRACT_NUMBER_RE,
    _parse_article,
    _parse_block,
)

ARTICLE_DATE = date(2026, 9, 2)
MIN_AMOUNT = 50_000_000.0


class TestAwardeeExtraction:
    """The awardee regex used to never match; the naive fallback ran instead."""

    @pytest.mark.parametrize(
        "block,expected",
        [
            (
                "Raytheon Co., Tucson, Arizona, is being awarded a $95,000,000 "
                "firm-fixed-price contract for missile systems.",
                "Raytheon Co.",
            ),
            (
                "LOCKHEED MARTIN CORP., Bethesda, Maryland, is being awarded a "
                "$1.2 billion contract for satellite work.",
                "LOCKHEED MARTIN CORP.",
            ),
            (
                "BAE Systems Land & Armaments L.P., Sterling Heights, Michigan, "
                "was awarded a $250,000,000 contract.",
                "BAE Systems Land & Armaments L.P.",
            ),
            (
                "Huntington Ingalls Industries, Newport News, Virginia, is being "
                "awarded $500 million for shipbuilding.",
                "Huntington Ingalls Industries",
            ),
        ],
    )
    def test_matches_house_style_blocks(self, block, expected):
        m = _AWARDEE_PATTERN.match(block)
        assert m is not None, "pattern should match DoD house style"
        assert m.group(1).strip() == expected

    def test_company_name_containing_a_comma_is_kept_whole(self):
        block = (
            "Kratos Defense & Security Solutions, Inc., San Diego, California, "
            "has been awarded a $78,000,000 contract for drone systems."
        )
        m = _AWARDEE_PATTERN.match(block)
        assert m is not None
        # The plain first-comma fallback would truncate to
        # "Kratos Defense & Security Solutions" and lose the "Inc.".
        assert m.group(1).strip() == "Kratos Defense & Security Solutions, Inc."

    def test_non_conforming_block_falls_through_to_fallback(self):
        block = "Some malformed block with no award verb at all, $60,000,000."
        assert _AWARDEE_PATTERN.match(block) is None


class TestContractNumberExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("under contract W912DY-26-C-0042 awarded today", "W912DY-26-C-0042"),
            ("N00019-26-D-0011 is a delivery order", "N00019-26-D-0011"),
            ("SPRPA1-26-C-1234 was issued", "SPRPA1-26-C-1234"),
        ],
    )
    def test_finds_real_contract_numbers(self, text, expected):
        m = _CONTRACT_NUMBER_RE.search(text)
        assert m is not None and m.group(1) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "competitively procured with proposals received",
            "indefinite-delivery indefinite-quantity contract",
            "This contract involves foreign military sales",
        ],
    )
    def test_prose_yields_no_contract_number(self, text):
        # The old `\w{10,20}` catch-all matched ordinary long words.
        assert _CONTRACT_NUMBER_RE.search(text) is None


class TestParseBlock:
    def test_extracts_award_and_obligated_amounts(self):
        block = (
            "Kratos Defense & Security Solutions, Inc., San Diego, California, "
            "has been awarded a maximum $95,000,000 indefinite-delivery "
            "indefinite-quantity contract for unmanned aerial drone systems. "
            "Obligated at the time of award: $18,000,000. "
            "Air Force Life Cycle Management Center is the contracting activity."
        )
        award = _parse_block(block, "https://example.gov/a", ARTICLE_DATE, MIN_AMOUNT)
        assert award is not None
        assert award.award_amount == 95_000_000.0
        assert award.obligated_amount == 18_000_000.0
        assert award.awardee_name_raw == "Kratos Defense & Security Solutions, Inc."
        assert award.is_idiq is True
        assert award.sub_agency == "Air Force"
        assert award.source == "defense_daily"
        assert award.posted_date == ARTICLE_DATE

    def test_block_below_threshold_is_skipped(self):
        block = (
            "Tiny Vendor LLC, Reston, Virginia, is being awarded a $2,000,000 "
            "contract for office supplies."
        )
        assert _parse_block(block, "https://example.gov/a", ARTICLE_DATE, MIN_AMOUNT) is None

    def test_block_with_no_amount_is_skipped(self):
        block = "General remarks about contracting policy with no figures."
        assert _parse_block(block, "https://example.gov/a", ARTICLE_DATE, MIN_AMOUNT) is None


ARTICLE_HTML = """
<html><body><article>
<p>CONTRACTS</p>
<p>Raytheon Co., Tucson, Arizona, is being awarded a $95,000,000 firm-fixed-price
contract for missile guidance systems. Obligated at the time of award: $20,000,000.
Navy is the contracting activity.</p>

<p>Tiny Vendor LLC, Reston, Virginia, is being awarded a $2,000,000 contract for
office supplies. Army is the contracting activity.</p>

<p>BAE Systems Land &amp; Armaments L.P., Sterling Heights, Michigan, was awarded a
$250,000,000 modification for combat vehicle support. Army is the contracting activity.</p>
</article></body></html>
"""


class TestParseArticle:
    def test_returns_only_blocks_above_threshold(self):
        awards = _parse_article(ARTICLE_HTML, "https://example.gov/article/1", ARTICLE_DATE)
        names = [a.awardee_name_raw for a in awards]
        assert "Raytheon Co." in names
        assert "BAE Systems Land & Armaments L.P." in names
        assert not any("Tiny Vendor" in n for n in names)

    def test_modification_flag_is_detected(self):
        awards = _parse_article(ARTICLE_HTML, "https://example.gov/article/1", ARTICLE_DATE)
        bae = next(a for a in awards if a.awardee_name_raw.startswith("BAE"))
        assert bae.is_modification is True

    def test_awards_carry_source_url_and_unique_ids(self):
        awards = _parse_article(ARTICLE_HTML, "https://example.gov/article/1", ARTICLE_DATE)
        assert all(a.source_url == "https://example.gov/article/1" for a in awards)
        assert len({a.source_award_id for a in awards}) == len(awards)

    def test_same_company_same_amount_different_contracts_do_not_collide(self):
        multi_html = """
        <html><body><article>
        <p>Raytheon Co., Tucson, Arizona, is being awarded a $75,000,000 contract N00019-26-C-0010 for Navy radar systems.</p>
        <p>Raytheon Co., Tucson, Arizona, is being awarded a $75,000,000 contract FA8620-26-C-0020 for Air Force sensor suites.</p>
        </article></body></html>
        """
        awards = _parse_article(multi_html, "https://example.gov/article/same-day", ARTICLE_DATE)
        assert len(awards) == 2
        assert awards[0].source_award_id != awards[1].source_award_id
        assert {a.contract_number for a in awards} == {"N00019-26-C-0010", "FA8620-26-C-0020"}

