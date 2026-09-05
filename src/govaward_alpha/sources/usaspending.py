"""USAspending.gov public API adapter.

Production API base: https://api.usaspending.gov
No authentication required.

This adapter serves two roles:
1. Confirmation/enrichment of awards found via SAM.gov or Defense.gov.
2. Backfill search for historical awards above the threshold.

API reference: https://api.usaspending.gov/docs/endpoints
Source reference (schema/terminology only): https://github.com/fedspendingtransparency/usaspending-api
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
    normalize_company_name,
    parse_dollar_amount,
)

logger = logging.getLogger(__name__)

_BASE = "https://api.usaspending.gov"
_SEARCH = f"{_BASE}/api/v2/search/spending_by_award/"

_HEADERS = {"Content-Type": "application/json"}
_PAGE_SIZE = 100
_MAX_PAGES = 50            # hard stop: 5,000 awards per search
_REQUEST_DELAY = 0.5       # seconds between pages, to stay a good citizen


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _post(client: httpx.Client, url: str, payload: dict) -> dict:
    resp = client.post(url, json=payload, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Recent award search (backfill / confirmation)
# ---------------------------------------------------------------------------

def _build_search_payload(
    start_date: str,
    end_date: str,
    min_amount: float,
    page: int = 1,
) -> dict:
    return {
        "filters": {
            # Filter by action_date (most-recent-transaction date) so we get
            # contracts that *just changed in the federal database*, not 5-year
            # IDIQs that happen to be in their performance period. This is the
            # alpha-gen lens — fresh transactions, not stale ongoing work.
            "time_period": [{
                "start_date": start_date,
                "end_date": end_date,
                "date_type": "action_date",
            }],
            "award_type_codes": ["A", "B", "C", "D"],  # contracts
            "award_amounts": [{"lower_bound": min_amount}],
        },
        "fields": [
            "Award ID",
            "Recipient Name",
            "recipient_id",
            "Start Date",
            "End Date",
            "Award Amount",
            "Awarding Agency",
            "Awarding Sub Agency",
            "Contract Award Type",
            "NAICS Code",
            "PSC Code",
            "Description",
            "Award Type",
            "Funding Agency",
            "Place of Performance State Code",
            "Last Modified Date",     # date USAspending most recently received this row
            "Base Obligation Date",
            "Recipient UEI",
            "base_and_all_options_value",
            "total_obligation",
        ],
        # Sort by Last Modified Date so freshest changes float to the top.
        # NOTE: "Action Date" is not a valid sort/field for spending_by_award —
        # it only exists on transaction-level endpoints. Last Modified Date is
        # the closest "fresh signal" we can sort on here.
        "sort": "Last Modified Date",
        "order": "desc",
        "page": page,
        "limit": _PAGE_SIZE,
        "subawards": False,
    }


def fetch_recent_awards(lookback_days: int | None = None) -> list[Award]:
    """Fetch recent contract awards from USAspending for confirmation/enrichment.

    USAspending data typically lags 7–14 days behind SAM.gov.
    We default to a wider lookback (14 days) to get useful results.
    """
    settings = get_settings()
    days = lookback_days if lookback_days is not None else max(settings.lookback_days, 14)
    today = date.today()
    start = (today - timedelta(days=days)).isoformat()
    end = today.isoformat()
    return _search_awards(start, end, settings.award_min_amount)


def backfill_awards(start_date: str, end_date: str) -> list[Award]:
    """Search historical awards between start_date and end_date (YYYY-MM-DD)."""
    settings = get_settings()
    return _search_awards(start_date, end_date, settings.award_min_amount)


def _search_awards(start_date: str, end_date: str, min_amount: float) -> list[Award]:
    awards: list[Award] = []
    page = 1

    # Force IPv4 (api.usaspending.gov, like SAM.gov, can have unreachable IPv6
    # endpoints from some networks).
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)
    with httpx.Client(transport=transport) as client:
        while True:
            payload = _build_search_payload(start_date, end_date, min_amount, page)
            try:
                data = _post(client, _SEARCH, payload)
            except Exception as exc:
                logger.error("USAspending search error: %s", exc)
                break

            results = data.get("results", [])
            if not results:
                break

            for item in results:
                award = _parse_result(item)
                if award:
                    awards.append(award)

            meta = data.get("page_metadata", {})
            has_next = meta.get("hasNext", False)
            logger.debug(
                "USAspending page %d: %d results, hasNext=%s",
                page, len(results), has_next,
            )
            if not has_next:
                break
            if page >= _MAX_PAGES:
                logger.warning(
                    "USAspending: stopping at the %d-page cap (%d awards) — "
                    "more results exist. Narrow the date range to see them.",
                    _MAX_PAGES, len(awards),
                )
                break
            page += 1
            time.sleep(_REQUEST_DELAY)

    logger.info("USAspending: retrieved %d awards (%s to %s)", len(awards), start_date, end_date)
    return awards


def _parse_result(item: dict[str, Any]) -> Award | None:
    settings = get_settings()

    award_id = item.get("Award ID") or item.get("generated_unique_award_id") or ""
    if not award_id:
        return None

    awardee_raw = item.get("Recipient Name") or ""
    amount_raw = item.get("Award Amount") or item.get("base_and_all_options_value") or 0
    try:
        award_amount = float(amount_raw)
    except (TypeError, ValueError):
        award_amount = parse_dollar_amount(str(amount_raw)) or 0.0

    if award_amount < settings.award_min_amount:
        return None

    obligated_raw = item.get("total_obligation") or 0
    try:
        obligated = float(obligated_raw) or None
    except (TypeError, ValueError):
        obligated = None

    start_date = _parse_date(item.get("Start Date"))
    end_date = _parse_date(item.get("End Date"))
    # Posted/award date should reflect *when this transaction hit the database*,
    # NOT the original obligation date of an old IDIQ. Priority:
    #   1. Last Modified Date — when USAspending most recently received this row
    #      (best freshness signal available on this endpoint)
    #   2. Base Obligation Date — original award date (worst case; only when nothing
    #      newer is available)
    base_date = (
        _parse_date(item.get("Last Modified Date"))
        or _parse_date(item.get("Base Obligation Date"))
    )

    agency = item.get("Awarding Agency") or ""
    sub_agency = item.get("Awarding Sub Agency") or ""
    description = item.get("Description") or ""
    naics = str(item.get("NAICS Code") or "")
    psc = str(item.get("PSC Code") or "")
    award_type = item.get("Award Type") or item.get("Contract Award Type") or ""

    flags = detect_flags(description, award_type)

    source_url = f"https://www.usaspending.gov/award/{award_id}/"

    return Award(
        source="usaspending",
        source_award_id=str(award_id),
        source_url=source_url,
        posted_date=base_date,
        award_date=base_date,
        awardee_name_raw=awardee_raw,
        awardee_name_normalized=normalize_company_name(awardee_raw),
        agency=agency,
        sub_agency=sub_agency,
        naics=naics,
        psc=psc,
        award_amount=award_amount,
        obligated_amount=obligated,
        description=description,
        contract_number="",
        award_type=award_type,
        period_start=start_date,
        period_end=end_date,
        is_idiq=flags["is_idiq"],
        is_modification=flags["is_modification"],
        is_option_exercise=flags["is_option_exercise"],
        is_ceiling_only=flags["is_ceiling_only"],
        content_hash=content_hash({
            "source": "usaspending",
            "award_id": str(award_id),
            "amount": award_amount,
            # Include posted_date so a transaction modification (with new
            # Last Modified Date) triggers an actual UPDATE rather than being
            # short-circuited by the upsert_award hash comparison.
            "posted": base_date.isoformat() if base_date else "",
        }),
    )


def _parse_date(val: str | None) -> date | None:
    if not val:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(val[:10], fmt).date()
        except ValueError:
            continue
    return None
