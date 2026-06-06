"""Defense.gov daily contracts page parser.

Fetches and parses DoD contract announcements from:
  https://www.defense.gov/News/Contracts/

Defense.gov blocks plain httpx/requests TLS fingerprints with a 403.
We use urllib with a custom SSL context (closer to browser TLS) as primary,
with httpx as fallback. Article URLs are discovered via the DoD sitemap XML,
which is typically not WAF-protected.
"""

from __future__ import annotations

import logging
import re
import ssl
import urllib.request
import urllib.error
from datetime import date, timedelta
from io import BytesIO
from typing import Any

from bs4 import BeautifulSoup

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

_INDEX_URL = "https://www.defense.gov/News/Contracts/"
_SITEMAP_URL = "https://www.defense.gov/sitemap.xml"
_CONTRACTS_SITEMAP = "https://www.defense.gov/news-sitemap.xml"

_BROWSER_HEADERS = [
    ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"),
    ("Accept-Language", "en-US,en;q=0.9"),
    ("Accept-Encoding", "gzip, deflate, br"),
    ("Connection", "keep-alive"),
    ("Upgrade-Insecure-Requests", "1"),
    ("Sec-Fetch-Dest", "document"),
    ("Sec-Fetch-Mode", "navigate"),
    ("Sec-Fetch-Site", "none"),
    ("Cache-Control", "max-age=0"),
]


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.set_ciphers(
        "ECDH+AESGCM:ECDH+CHACHA20:DH+AESGCM:DH+CHACHA20:ECDH+AES256:DH+AES256:"
        "ECDH+AES128:DH+AES:ECDH+HIGH:DH+HIGH:RSA+AESGCM:RSA+AES:RSA+HIGH:!aNULL:!eNULL:!MD5"
    )
    return ctx


def _fetch_url(url: str, timeout: int = 25) -> bytes:
    """Fetch a URL using urllib with browser-like TLS/headers. Returns raw bytes."""
    req = urllib.request.Request(url)
    for key, val in _BROWSER_HEADERS:
        req.add_header(key, val)
    ctx = _ssl_context()
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return resp.read()


def _fetch_text(url: str, timeout: int = 25) -> str:
    """Fetch URL and decode to text, handling gzip transparently."""
    raw = _fetch_url(url, timeout)
    # urllib auto-decompresses gzip when Accept-Encoding includes it
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return raw.decode("latin-1", errors="replace")


# ---------------------------------------------------------------------------
# Article URL discovery
# ---------------------------------------------------------------------------

def _discover_via_sitemap(lookback_days: int) -> list[tuple[str, date]]:
    """Try to find contract article URLs from DoD sitemap/news-sitemap."""
    cutoff = date.today() - timedelta(days=lookback_days)
    results: list[tuple[str, date]] = []

    for sitemap_url in [_CONTRACTS_SITEMAP, _SITEMAP_URL]:
        try:
            text = _fetch_text(sitemap_url, timeout=20)
        except Exception as exc:
            logger.debug("Sitemap fetch failed (%s): %s", sitemap_url, exc)
            continue

        soup = BeautifulSoup(text, "lxml-xml") if "xml" in sitemap_url else BeautifulSoup(text, "lxml")

        # Look for <loc> entries pointing to contract articles
        for loc in soup.find_all("loc"):
            url = loc.get_text(strip=True)
            if "/News/Contracts/Contract/Article/" not in url:
                continue
            # Try to extract lastmod date
            lastmod = loc.find_next_sibling("lastmod")
            article_date = None
            if lastmod:
                article_date = _parse_iso_date(lastmod.get_text(strip=True))
            if article_date and article_date < cutoff:
                continue
            results.append((url, article_date or date.today()))

        if results:
            logger.debug("Sitemap %s: found %d contract article URLs", sitemap_url, len(results))
            return results

    return results


def _discover_via_index(lookback_days: int) -> list[tuple[str, date]]:
    """Scrape the contracts index page to find article URLs."""
    cutoff = date.today() - timedelta(days=lookback_days)
    try:
        text = _fetch_text(_INDEX_URL)
    except Exception as exc:
        logger.error("Defense.gov index fetch failed: %s", exc)
        return []

    soup = BeautifulSoup(text, "lxml")
    results: list[tuple[str, date]] = []
    seen: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if "/News/Contracts/Contract/Article/" not in href:
            continue
        full_url = href if href.startswith("http") else f"https://www.defense.gov{href}"
        if full_url in seen:
            continue
        seen.add(full_url)
        article_date = _extract_date_near_link(a_tag)
        if article_date and article_date < cutoff:
            continue
        results.append((full_url, article_date or date.today()))

    return results


def _discover_article_urls(lookback_days: int) -> list[tuple[str, date]]:
    """Try sitemap first, then index page scrape."""
    results = _discover_via_sitemap(lookback_days)
    if results:
        return results
    return _discover_via_index(lookback_days)


def _extract_date_near_link(tag: Any) -> date | None:
    parent = tag.find_parent(["li", "div", "article", "p"])
    if not parent:
        return None
    return _parse_date_from_text(parent.get_text(" ", strip=True))


_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date_from_text(text: str) -> date | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    month = _MONTH_MAP.get(m.group(1)[:3].lower())
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(2)))
    except ValueError:
        return None


def _parse_iso_date(val: str) -> date | None:
    try:
        return date.fromisoformat(val[:10])
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Article parser
# ---------------------------------------------------------------------------

_COMPANY_BLOCK_SPLIT = re.compile(r"\n{2,}|\r\n{2,}")
_AWARD_AMOUNT_LINE = re.compile(
    r"\$[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|[mMbBkK]))?",
    re.IGNORECASE,
)
_AWARDEE_PATTERN = re.compile(
    r"^([A-Z][A-Z\s,\.&'/-]{3,80?}?)(?:,|\s+(?:is\s+being|has\s+been|was\s+awarded))",
)
_CONTRACT_NUMBER_RE = re.compile(
    r"\b([A-Z]{1,2}\d{2}[A-Z]{1,2}-\d{2,}-[A-Z]-\d{4,}|[A-Z]{3,6}-\d{2,}-[A-Z]-\d{4,}|\w{10,20})\b"
)


def _parse_article(html: str, source_url: str, article_date: date) -> list[Award]:
    settings = get_settings()
    soup = BeautifulSoup(html, "lxml")

    body = (
        soup.find("div", class_=re.compile(r"body|content|article", re.I))
        or soup.find("article")
        or soup.find("main")
        or soup
    )
    text = body.get_text("\n", strip=True) if body else soup.get_text("\n", strip=True)

    awards: list[Award] = []
    blocks = [b.strip() for b in _COMPANY_BLOCK_SPLIT.split(text) if b.strip()]

    for block in blocks:
        if not _AWARD_AMOUNT_LINE.search(block):
            continue
        award = _parse_block(block, source_url, article_date, settings.award_min_amount)
        if award is not None:
            awards.append(award)

    return awards


def _parse_block(block: str, source_url: str, article_date: date, min_amount: float) -> Award | None:
    amounts = [parse_dollar_amount(m.group()) for m in _AWARD_AMOUNT_LINE.finditer(block)]
    amounts = [a for a in amounts if a is not None]
    if not amounts:
        return None

    award_amount = max(amounts)
    if award_amount < min_amount:
        return None

    obligated = extract_obligated_amount(block)
    if obligated is None and len(amounts) >= 2:
        sorted_amounts = sorted(amounts)
        if sorted_amounts[0] != award_amount and sorted_amounts[0] > 0:
            obligated = sorted_amounts[0]

    awardee_raw = ""
    m = _AWARDEE_PATTERN.match(block)
    if m:
        awardee_raw = m.group(1).strip().rstrip(",").strip()
    else:
        first_line = block.split("\n")[0]
        awardee_raw = first_line.split(",")[0].strip()

    agency = "Department of Defense"
    sub_agency = ""
    for branch in ("Navy", "Air Force", "Army", "Marine Corps", "Defense Logistics Agency",
                   "Defense Advanced Research Projects Agency", "DARPA", "Space Force",
                   "Missile Defense Agency", "National Geospatial-Intelligence Agency"):
        if branch.lower() in block.lower():
            sub_agency = branch
            break

    contract_number = ""
    cn_match = _CONTRACT_NUMBER_RE.search(block)
    if cn_match:
        contract_number = cn_match.group(1)

    description = block[:1500]
    flags = detect_flags(description)

    source_id = content_hash({
        "url": source_url,
        "awardee": awardee_raw,
        "amount": award_amount,
        "date": str(article_date),
    })

    return Award(
        source="defense_daily",
        source_award_id=source_id,
        source_url=source_url,
        posted_date=article_date,
        award_date=article_date,
        awardee_name_raw=awardee_raw,
        awardee_name_normalized=normalize_company_name(awardee_raw),
        agency=agency,
        sub_agency=sub_agency,
        award_amount=award_amount,
        obligated_amount=obligated,
        description=description,
        contract_number=contract_number,
        award_type="Award Notice",
        is_idiq=flags["is_idiq"],
        is_modification=flags["is_modification"],
        is_option_exercise=flags["is_option_exercise"],
        is_ceiling_only=flags["is_ceiling_only"],
        content_hash=content_hash({
            "source": "defense_daily",
            "awardee": awardee_raw,
            "amount": award_amount,
            "description": description[:300],
        }),
    )


def fetch_awards(lookback_days: int | None = None) -> list[Award]:
    """Fetch and parse DoD contract awards from the last N days."""
    settings = get_settings()
    days = lookback_days if lookback_days is not None else settings.lookback_days

    all_awards: list[Award] = []
    article_urls = _discover_article_urls(days)

    if not article_urls:
        logger.warning(
            "Defense.gov: no article URLs found. The site may be blocking automated access. "
            "Try running scan-defense manually later or check https://www.defense.gov/News/Contracts/ in a browser."
        )
        return []

    for url, article_date in article_urls:
        try:
            text = _fetch_text(url)
            awards = _parse_article(text, url, article_date)
            logger.debug("Defense.gov %s → %d awards", url, len(awards))
            all_awards.extend(awards)
        except Exception as exc:
            logger.warning("Defense.gov article parse error [%s]: %s", url, exc)

    logger.info(
        "Defense.gov: fetched %d qualifying awards across %d articles",
        len(all_awards), len(article_urls),
    )
    return all_awards
