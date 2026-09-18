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
from pathlib import Path
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
    ("Accept-Encoding", "gzip, deflate"),
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
# DoD blocks open with the awardee, then a place of performance, then the
# award verb: "Raytheon Co., Tucson, Arizona, is being awarded ...".
# The name runs up to the first comma; the verb clause anchors the match so a
# block that does not follow the house style falls through to the plain
# first-comma split rather than capturing something arbitrary.
# NOTE: the character class must allow lower case — DoD mixes "Raytheon Co."
# and "LOCKHEED MARTIN CORP." in the same page.
_AWARDEE_PATTERN = re.compile(
    r"^([A-Za-z][A-Za-z0-9\s,\.&'/-]{3,120}?)"
    r"\s*,\s*[^,]{2,60},"                      # city (and the comma after it)
    r"[^,]{0,60},?\s*"                         # optional state / country
    r"(?:is\s+being|has\s+been|was)\s+awarded",
)

# Real DoD contract numbers, e.g. "W912DY-26-C-0042", "N00019-26-D-0011",
# "SPRPA1-26-C-1234". The previous catch-all `\w{10,20}` alternative matched
# almost any long word, so ordinary prose became a "contract number".
_CONTRACT_NUMBER_RE = re.compile(
    r"\b([A-Z]{1,2}\d{2,5}[A-Z]{0,2}-\d{2}-[A-Z]-\d{3,5}"
    r"|[A-Z]{3,6}\d?-\d{2}-[A-Z]-\d{3,5})\b"
)


def _split_blocks(body: Any) -> list[str]:
    """Split a contracts article into one text block per award.

    Defense.gov puts each company's award in its own ``<p>``, so paragraphs
    are the real record boundary. Falling back to a blank-line split of the
    whole article does not work on this markup: ``get_text("\\n")`` joins
    elements with a *single* newline, so a blank-line pattern matches nothing
    and the entire page collapses into one block — which then yields one
    award per article (with the page's largest dollar figure) instead of one
    per company. Paragraph splitting first, blank lines only as a fallback
    for markup without ``<p>`` tags.
    """
    paragraphs = [
        p.get_text(" ", strip=True) for p in body.find_all("p")
    ] if hasattr(body, "find_all") else []
    blocks = [p for p in paragraphs if p]
    if blocks:
        return blocks

    text = body.get_text("\n", strip=True)
    return [b.strip() for b in _COMPANY_BLOCK_SPLIT.split(text) if b.strip()]


def _parse_article(html: str, source_url: str, article_date: date) -> list[Award]:
    settings = get_settings()
    soup = BeautifulSoup(html, "lxml")

    body = (
        soup.find("div", class_=re.compile(r"body|content|article", re.I))
        or soup.find("article")
        or soup.find("main")
        or soup
    )
    awards: list[Award] = []
    blocks = _split_blocks(body or soup)

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
    flags = detect_flags(
        description,
        contract_number=contract_number,
        award_date=article_date,
    )

    source_id = content_hash({
        "url": source_url,
        "awardee": awardee_raw,
        "amount": award_amount,
        "contract_number": contract_number,
        "sub_agency": sub_agency,
        "desc_slug": description[:120].strip().lower(),
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
        is_legacy_contract=flags.get("is_legacy_contract", False),
        content_hash=content_hash({
            "source": "defense_daily",
            "awardee": awardee_raw,
            "amount": award_amount,
            "description": description[:300],
        }),
    )


def fetch_awards(
    lookback_days: int | None = None,
    file_path: str | Path | None = None,
) -> list[Award]:
    """Fetch and parse DoD contract awards.

    Supports direct local HTML file ingest (e.g. from headless browser, manual save,
    or test fixture) via ``file_path``, falling back to network discovery.
    """
    if file_path:
        p = Path(file_path)
        if not p.exists():
            logger.error("Defense.gov ingest file not found: %s", file_path)
            return []
        text = p.read_text(encoding="utf-8", errors="replace")
        today = date.today()
        awards = _parse_article(text, str(p), today)
        logger.info("Defense.gov: parsed %d awards from local file %s", len(awards), p.name)
        return awards

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
