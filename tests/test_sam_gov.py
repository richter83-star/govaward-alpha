"""SAM.gov adapter tests — HTTP mocked with pytest-httpx, no network."""

from __future__ import annotations

import re
from datetime import date

import pytest

from govaward_alpha.sources import sam_gov
from govaward_alpha.sources.sam_gov import _parse_date, _parse_opportunity, fetch_awards

BASE_URL = "https://api.sam.gov/opportunities/v2/search"
# pytest-httpx matches the full URL including the query string; the adapter
# appends api_key/date/paging params, so match on a prefix pattern.
ANY_SEARCH = re.compile(r"^https://api\.sam\.gov/opportunities/v2/search.*")


def _opp(notice_id: str, amount, **extra):
    base = {
        "noticeId": notice_id,
        "awardee": {"name": "Acme Defense Corp"},
        "baseAndAllOptionsValue": amount,
        "description": "Firm fixed price award for radar systems.",
        "postedDate": "2026-09-02",
        "fullParentPathName": "DEPT OF DEFENSE",
        "naicsCode": "334511",
        "pscCode": "5840",
        "type": "Award Notice",
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _no_page_delay(monkeypatch):
    monkeypatch.setattr(sam_gov, "_REQUEST_DELAY", 0)


class TestParseOpportunity:
    def test_maps_fields_onto_award(self):
        award = _parse_opportunity(_opp("N1", "95000000"))
        assert award is not None
        assert award.source == "sam_gov"
        assert award.source_award_id == "N1"
        assert award.award_amount == 95_000_000.0
        assert award.awardee_name_raw == "Acme Defense Corp"
        assert award.agency == "DEPT OF DEFENSE"
        assert award.source_url == "https://sam.gov/opp/N1/view"
        assert award.posted_date == date(2026, 9, 2)

    def test_below_threshold_is_dropped(self):
        assert _parse_opportunity(_opp("N2", "1000000")) is None

    def test_missing_notice_id_is_dropped(self):
        opp = _opp("", "95000000")
        opp.pop("noticeId")
        assert _parse_opportunity(opp) is None

    def test_awardee_given_as_plain_string(self):
        award = _parse_opportunity(_opp("N3", "95000000", awardee="Globex Systems LLC"))
        assert award is not None
        assert award.awardee_name_raw == "Globex Systems LLC"

    def test_obligated_amount_read_from_description(self):
        opp = _opp(
            "N4",
            "95000000",
            description="Maximum $95,000,000 IDIQ. Obligated at the time of award: $18,000,000.",
        )
        award = _parse_opportunity(opp)
        assert award.obligated_amount == 18_000_000.0
        assert award.is_idiq is True

    def test_flags_detected_from_description(self):
        opp = _opp("N5", "95000000", description="Modification exercising an option period.")
        award = _parse_opportunity(opp)
        assert award.is_modification is True
        assert award.is_option_exercise is True


class TestParseDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-09-02", date(2026, 9, 2)),
            ("2026-09-02T13:45:00", date(2026, 9, 2)),
            ("09/02/2026", date(2026, 9, 2)),
            ("", None),
            (None, None),
            ("not-a-date", None),
        ],
    )
    def test_parses_known_formats(self, raw, expected):
        assert _parse_date(raw) == expected


class TestFetchAwards:
    def test_single_page(self, httpx_mock):
        httpx_mock.add_response(
            url=ANY_SEARCH,
            json={
                "opportunitiesData": [_opp("N1", "95000000"), _opp("N2", "80000000")],
                "totalRecords": 2,
            },
        )
        awards = fetch_awards(lookback_days=1)
        assert [a.source_award_id for a in awards] == ["N1", "N2"]

    def test_skipped_when_api_key_missing(self, monkeypatch):
        from govaward_alpha.config import get_settings

        monkeypatch.setattr(get_settings(), "sam_api_key", "")
        # No httpx_mock here on purpose: the adapter must return before it
        # ever builds a request.
        assert fetch_awards(lookback_days=1) == []

    def test_http_error_returns_empty_rather_than_raising(self, httpx_mock, monkeypatch):
        from tenacity import wait_none

        monkeypatch.setattr(sam_gov._fetch_page.retry, "wait", wait_none())
        httpx_mock.add_response(url=ANY_SEARCH, status_code=403, is_reusable=True)
        assert fetch_awards(lookback_days=1) == []

    def test_body_level_throttle_is_treated_as_an_error(self, httpx_mock, monkeypatch):
        """SAM returns a 429 payload inside a 200 response; it must not parse as data."""
        from tenacity import wait_none

        monkeypatch.setattr(sam_gov._fetch_page.retry, "wait", wait_none())
        monkeypatch.setattr(sam_gov.time, "sleep", lambda *_: None)
        httpx_mock.add_response(
            url=ANY_SEARCH,
            json={"code": 429, "message": "rate limit", "nextAccessTime": "later"},
            is_reusable=True,
        )
        assert fetch_awards(lookback_days=1) == []

    @pytest.mark.parametrize(
        "placeholder",
        ["your_sam_api_key_here", "<insert_key>", "TEST", "xxx", "none", ""],
    )
    def test_skipped_when_api_key_is_placeholder(self, monkeypatch, placeholder):
        from govaward_alpha.config import get_settings

        monkeypatch.setattr(get_settings(), "sam_api_key", placeholder)
        assert fetch_awards(lookback_days=1) == []

    def test_404_fails_fast_without_retry(self, httpx_mock):
        """HTTP 404 should fail immediately without retrying."""
        httpx_mock.add_response(url=ANY_SEARCH, status_code=404)
        assert fetch_awards(lookback_days=1) == []
        # Exactly one request made — tenacity should not retry 404
        assert len(httpx_mock.get_requests()) == 1
