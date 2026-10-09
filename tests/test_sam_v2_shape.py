"""SAM.gov Opportunities v2: nested award object, description-is-a-URL, implausible amounts."""

from __future__ import annotations

from datetime import date

from govaward_alpha.sources.sam_gov import _parse_opportunity

BASE = {
    "noticeId": "abc123",
    "type": "Award Notice",
    "postedDate": "2026-10-07",
    "fullParentPathName": "DEPT OF DEFENSE.DEPT OF THE ARMY",
    "organizationName": "DEPT OF THE ARMY",
    "description": "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=78986000000000",
    "solicitationNumber": "W912-26-R-0001",
}


def test_nested_award_object_supplies_awardee_amount_and_number():
    opp = dict(BASE, award={"date": "2026-10-06", "number": "W912DY-26-C-0042", "amount": "95000000",
                            "awardee": {"name": "Kratos Defense & Security Solutions, Inc."}})
    a = _parse_opportunity(opp)
    assert a.awardee_name_raw == "Kratos Defense & Security Solutions, Inc."
    assert a.award_amount == 95_000_000.0
    assert a.contract_number == "W912DY-26-C-0042"
    assert a.award_date == date(2026, 10, 6)
    assert a.description == ""          # the URL is not prose


def test_description_url_never_becomes_an_amount_and_agency_is_not_the_awardee():
    a = _parse_opportunity(dict(BASE))
    assert a is None or (a.award_amount is None and a.awardee_name_raw == "")


def test_implausible_amount_is_dropped():
    opp = dict(BASE, award={"amount": "78986000000000", "awardee": {"name": "X Corp"}})
    a = _parse_opportunity(opp)
    assert a is None or a.award_amount is None
