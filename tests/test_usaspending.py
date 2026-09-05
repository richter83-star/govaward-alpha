"""USAspending adapter tests — HTTP mocked with pytest-httpx, no network."""

from __future__ import annotations

from datetime import date

import pytest

from govaward_alpha.sources import usaspending
from govaward_alpha.sources.usaspending import _parse_result, _search_awards

SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"


def _result(award_id: str, amount: float, name: str = "Acme Defense Corp", **extra):
    base = {
        "Award ID": award_id,
        "Recipient Name": name,
        "Award Amount": amount,
        "total_obligation": amount / 2,
        "Awarding Agency": "Department of Defense",
        "Awarding Sub Agency": "Department of the Navy",
        "Description": "Firm fixed price contract for radar systems",
        "NAICS Code": "334511",
        "PSC Code": "5840",
        "Award Type": "DEFINITIVE CONTRACT",
        "Start Date": "2026-09-01",
        "End Date": "2029-08-31",
        "Last Modified Date": "2026-09-02",
        "Base Obligation Date": "2026-08-15",
    }
    base.update(extra)
    return base


def _page(results, has_next=False):
    return {"results": results, "page_metadata": {"hasNext": has_next}}


class TestParseResult:
    def test_maps_fields_onto_award(self):
        award = _parse_result(_result("CONT_AWD_1", 95_000_000.0))
        assert award is not None
        assert award.source == "usaspending"
        assert award.source_award_id == "CONT_AWD_1"
        assert award.award_amount == 95_000_000.0
        assert award.obligated_amount == 47_500_000.0
        assert award.agency == "Department of Defense"
        assert award.naics == "334511"
        assert award.period_start == date(2026, 9, 1)
        assert award.source_url.endswith("/award/CONT_AWD_1/")

    def test_prefers_last_modified_date_for_freshness(self):
        award = _parse_result(_result("CONT_AWD_2", 60_000_000.0))
        assert award.posted_date == date(2026, 9, 2)

    def test_falls_back_to_base_obligation_date(self):
        item = _result("CONT_AWD_3", 60_000_000.0)
        item["Last Modified Date"] = None
        award = _parse_result(item)
        assert award.posted_date == date(2026, 8, 15)

    def test_below_threshold_is_dropped(self):
        assert _parse_result(_result("CONT_AWD_4", 1_000_000.0)) is None

    def test_missing_award_id_is_dropped(self):
        item = _result("", 95_000_000.0)
        assert _parse_result(item) is None

    def test_content_hash_changes_when_transaction_is_modified(self):
        a = _parse_result(_result("CONT_AWD_5", 95_000_000.0))
        modified = _result("CONT_AWD_5", 95_000_000.0)
        modified["Last Modified Date"] = "2026-09-03"
        b = _parse_result(modified)
        assert a.content_hash != b.content_hash


class TestSearchAwards:
    def test_single_page(self, httpx_mock):
        httpx_mock.add_response(
            url=SEARCH_URL,
            json=_page([_result("A1", 95_000_000.0), _result("A2", 80_000_000.0)]),
        )
        awards = _search_awards("2026-09-01", "2026-09-04", 50_000_000.0)
        assert [a.source_award_id for a in awards] == ["A1", "A2"]

    def test_follows_pagination(self, httpx_mock, monkeypatch):
        monkeypatch.setattr(usaspending, "_REQUEST_DELAY", 0)
        httpx_mock.add_response(url=SEARCH_URL, json=_page([_result("A1", 95_000_000.0)], has_next=True))
        httpx_mock.add_response(url=SEARCH_URL, json=_page([_result("A2", 90_000_000.0)], has_next=False))
        awards = _search_awards("2026-09-01", "2026-09-04", 50_000_000.0)
        assert [a.source_award_id for a in awards] == ["A1", "A2"]

    def test_stops_at_the_page_cap(self, httpx_mock, monkeypatch):
        """An always-hasNext API must not paginate forever."""
        monkeypatch.setattr(usaspending, "_REQUEST_DELAY", 0)
        monkeypatch.setattr(usaspending, "_MAX_PAGES", 3)
        httpx_mock.add_response(
            url=SEARCH_URL,
            json=_page([_result("A1", 95_000_000.0)], has_next=True),
            is_reusable=True,
        )
        awards = _search_awards("2026-09-01", "2026-09-04", 50_000_000.0)
        assert len(awards) == 3, "should stop after _MAX_PAGES pages"

    def test_empty_result_set(self, httpx_mock):
        httpx_mock.add_response(url=SEARCH_URL, json=_page([]))
        assert _search_awards("2026-09-01", "2026-09-04", 50_000_000.0) == []

    def test_http_error_returns_empty_rather_than_raising(self, httpx_mock, monkeypatch):
        from tenacity import wait_none

        # Skip the real exponential backoff so the test stays fast.
        monkeypatch.setattr(usaspending._post.retry, "wait", wait_none())
        httpx_mock.add_response(url=SEARCH_URL, status_code=500, is_reusable=True)
        assert _search_awards("2026-09-01", "2026-09-04", 50_000_000.0) == []
