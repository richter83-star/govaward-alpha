"""SAM.gov Opportunities API adapter.

Endpoint: https://api.sam.gov/opportunities/v2/search
Auth: api_key query parameter — key must be from your SAM.gov account
      (sam.gov → sign in → avatar → API Key Management).

Rate limits: ~1 req/sec sustained; body-level throttle returns
  {"code":429, "message":"...", "nextAccessTime":"..."} on 200 responses.
We respect this with a 1.2s inter-page delay and body-level throttle detection.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from govaward_alpha.config import get_settings
from govaward_alpha.models import Award
from govaward_alpha.normalize import (
    content_hash,
    detect_flags,
    extract_obligated_amount,
    normalize_company_name,
    parse_dollar_amount,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.sam.gov/opportunities/v2/search"
_PAGE_SIZE = 100
_REQUEST_DELAY = 1.2  # seconds between pages to stay under rate limit


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    reraise=True,
)
def _fetch_page(
    client: httpx.Client,
    api_key: str,
    posted_from: str,
    posted_to: str,
    offset: int,
) -> dict[str, Any]:
    params = {
        "api_key": api_key,
        "postedFrom": posted_from,
        "postedTo": posted_to,
        "limit": _PAGE_SIZE,
        "offset": offset,
        "ptype": "a",  # Award notices only
    }
    resp = client.get(_BASE_URL, params=params, timeout=30)

    # SAM.gov sometimes returns 429 or body-level throttle on 200
    if resp.status_code == 429:
        raise httpx.HTTPStatusError("429 Too Many Requests", request=resp.request, response=resp)

    resp.raise_for_status()
    data = resp.json()

    # Body-level rate limit: {"code": 429, "message": "...", "nextAccessTime": "..."}
    if "opportunitiesData" not in data and data.get("code") in (429, "429"):
        next_access = data.get("nextAccessTime", "unknown")
        logger.warning("SAM.gov body-level throttle hit. nextAccessTime=%s — backing off 15s", next_access)
        time.sleep(15)
        raise httpx.HTTPStatusError("Body-level 429", request=resp.request, response=resp)

    return data


def fetch_awards(lookback_days: int | None = None) -> list[Award]:
    """
    Fetch SAM.gov Award Notices posted in the last N days.
    Applies award_min_amount filter where amount is parseable.
    """
    settings = get_settings()
    if not settings.sam_api_key:
        logger.warning("SAM_API_KEY not set — skipping SAM.gov source.")
        return []

    days = lookback_days if lookback_days is not None else settings.lookback_days
    today = date.today()
    posted_from = (today - timedelta(days=days)).strftime("%m/%d/%Y")
    posted_to = today.strftime("%m/%d/%Y")

    awards: list[Award] = []
    offset = 0

    # Force IPv4: api.sam.gov publishes AAAA records but its IPv6 endpoints are
    # frequently unreachable from networks where IPv4 works fine (Windows hosts
    # behind Hyper-V, some ISPs, etc.). Binding the local socket to an IPv4
    # interface forces httpx to use the A records exclusively.
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)
    with httpx.Client(transport=transport) as client:
        while True:
            try:
                data = _fetch_page(client, settings.sam_api_key, posted_from, posted_to, offset)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else 0
                if status == 404:
                    logger.error(
                        "SAM.gov 404. Your key must come from your SAM.gov account "
                        "(sam.gov → sign in → avatar → API Key Management), "
                        "not from api.data.gov. New keys may take up to 1 hour to activate."
                    )
                elif status == 403:
                    logger.error("SAM.gov 403: key invalid or not authorised.")
                else:
                    logger.error("SAM.gov HTTP error: %s", exc)
                break
            except Exception as exc:
                logger.error("SAM.gov fetch error: %s", exc)
                break

            opportunities = data.get("opportunitiesData", [])
            total = data.get("totalRecords", 0)
            logger.debug(
                "SAM.gov page offset=%d total=%d returned=%d",
                offset, total, len(opportunities),
            )

            for opp in opportunities:
                award = _parse_opportunity(opp)
                if award is not None:
                    awards.append(award)

            offset += _PAGE_SIZE
            if offset >= total or not opportunities:
                break

            # Respect rate limit between pages
            time.sleep(_REQUEST_DELAY)

    logger.info("SAM.gov: fetched %d qualifying awards", len(awards))
    return awards


def _parse_opportunity(opp: dict) -> Award | None:
    settings = get_settings()

    # SAM.gov award notices: amount lives in awardee sub-object or top-level fields
    awardee_obj = opp.get("awardee") or {}
    if isinstance(awardee_obj, str):
        awardee_raw = awardee_obj
        awardee_obj = {}
    else:
        awardee_raw = (
            awardee_obj.get("name")
            or opp.get("awardeeName")
            or opp.get("organizationName")
            or ""
        )

    # Amount fields — SAM award notices put the amount in several possible fields
    raw_amount_str = (
        opp.get("baseAndAllOptionsValue")
        or opp.get("baseAndExercisedOptionsValue")
        or awardee_obj.get("amount")
        or opp.get("awardAmount")
        or opp.get("dollarValue")
        or ""
    )
    award_amount = parse_dollar_amount(str(raw_amount_str)) if raw_amount_str else None

    description = opp.get("description") or ""

    # If no top-level amount, try parsing from description prose
    if award_amount is None and description:
        award_amount = parse_dollar_amount(description)

    # Filter: skip if we have a definite amount below threshold
    if award_amount is not None and award_amount < settings.award_min_amount:
        return None

    # Parse obligated amount from description
    obligated = extract_obligated_amount(description)
    obl_field = opp.get("obligatedAmount") or opp.get("currentValue")
    if obl_field:
        parsed_obl = parse_dollar_amount(str(obl_field))
        if parsed_obl is not None:
            obligated = parsed_obl

    # Dates
    posted_date = _parse_date(opp.get("postedDate"))
    award_date = _parse_date(opp.get("awardDate") or opp.get("responseDeadLine"))

    # Agency
    agency = (
        opp.get("fullParentPathName")
        or opp.get("organizationName")
        or opp.get("department")
        or ""
    )
    if isinstance(agency, dict):
        agency = agency.get("name") or ""
    sub_agency = opp.get("subtierName") or opp.get("agency") or ""

    # Source ID
    source_id = str(
        opp.get("opportunityId")
        or opp.get("noticeId")
        or opp.get("solicitationNumber")
        or ""
    )
    if not source_id:
        return None

    source_url = f"https://sam.gov/opp/{source_id}/view"
    flags = detect_flags(description, opp.get("type") or "")

    return Award(
        source="sam_gov",
        source_award_id=source_id,
        source_url=source_url,
        posted_date=posted_date,
        award_date=award_date,
        awardee_name_raw=awardee_raw,
        awardee_name_normalized=normalize_company_name(awardee_raw),
        agency=agency,
        sub_agency=sub_agency,
        naics=opp.get("naicsCode") or "",
        psc=opp.get("pscCode") or "",
        award_amount=award_amount,
        obligated_amount=obligated,
        description=description,
        contract_number=opp.get("solicitationNumber") or "",
        award_type=opp.get("type") or "Award Notice",
        is_idiq=flags["is_idiq"],
        is_modification=flags["is_modification"],
        is_option_exercise=flags["is_option_exercise"],
        is_ceiling_only=flags["is_ceiling_only"],
        content_hash=content_hash({
            "source": "sam_gov",
            "source_award_id": source_id,
            "award_amount": award_amount,
            "awardee_name_raw": awardee_raw,
            "description": description[:500],
        }),
    )


def _parse_date(val: str | None) -> date | None:
    if not val:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(val[:19], fmt[:len(val[:19])]).date()
        except ValueError:
            continue
    return None
