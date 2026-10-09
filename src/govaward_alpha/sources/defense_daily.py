"""DoD daily contracts (war.gov, formerly defense.gov) — discovery + parser.

Discovery uses the official contracts RSS feed, which war.gov publishes for
automated readers:
  https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945&max=10

Article pages are fetched with a plain, honestly-labelled client. (The old
browser-impersonating headers were answered with 403 on every request.) The
site's bot wall can still refuse a request; we never try to get around it.
A 403 raises ``DefenseSiteBlocked`` once, with no retries, and the operator
supplies the page instead: save it into ``data/pentagon_drops/`` or paste it
into the dashboard, and ``parse_drop`` turns it into awards.
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

RSS_URL = "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945&max=10"
_ARTICLE_PATH = "/News/Contracts/Contract/Article/"

# An honest, identifiable client. We read the public RSS feed a few times per
# weekday evening and nothing else.
_HEADERS = [
    ("User-Agent", "GovAwardAlpha/0.2 (public-contract research; low-volume RSS reader)"),
    ("Accept", "application/rss+xml,application/xml,text/html;q=0.9,*/*;q=0.5"),
    ("Accept-Language", "en-US,en;q=0.9"),
]

DROP_HINT = (
    "Open the article in your browser, then either paste the page text into the dashboard "
    "(DoD drop box) or save the page as HTML into data/pentagon_drops/."
)


class DefenseSiteBlocked(RuntimeError):
    """war.gov refused an automated article fetch (HTTP 403). ``url`` is the article."""

    def __init__(self, url: str, article_date: date | None = None) -> None:
        self.url = url
        self.article_date = article_date
        super().__init__(f"war.gov blocks automated article fetches (403): {url}. {DROP_HINT}")


def _fetch_url(url: str, timeout: int = 25) -> bytes:
    """Fetch a URL with urllib and the default TLS context. Returns raw bytes."""
    req = urllib.request.Request(url)
    for key, val in _HEADERS:
        req.add_header(key, val)
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=timeout) as resp:
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

def parse_rss(xml_text: str, lookback_days: int, today: date | None = None) -> list[tuple[str, date]]:
    """Contract-article (url, date) pairs from the war.gov RSS feed, newest first.

    The date comes from the title ("Contracts for Oct. 8, 2026"); the RSS
    pubDate (GMT, ~21:00) is the fallback, converted to US Eastern.
    """
    from email.utils import parsedate_to_datetime

    from govaward_alpha.timeutil import get_eastern_time

    cutoff = (today or date.today()) - timedelta(days=lookback_days)
    soup = BeautifulSoup(xml_text, "lxml-xml")
    results: list[tuple[str, date]] = []
    seen: set[str] = set()
    for item in soup.find_all("item"):
        link = item.find("link")
        url = link.get_text(strip=True) if link else ""
        if _ARTICLE_PATH not in url or url in seen:
            continue
        seen.add(url)
        title = item.find("title")
        article_date = _parse_date_from_text(title.get_text(" ", strip=True)) if title else None
        if article_date is None and item.find("pubDate"):
            try:
                article_date = get_eastern_time(parsedate_to_datetime(item.find("pubDate").get_text(strip=True))).date()
            except (TypeError, ValueError):
                article_date = None
        article_date = article_date or (today or date.today())
        if article_date < cutoff:
            continue
        results.append((url, article_date))
    results.sort(key=lambda t: t[1], reverse=True)
    return results


def _discover_article_urls(lookback_days: int) -> list[tuple[str, date]]:
    """Contract articles from the official RSS feed (the only automated-friendly endpoint)."""
    try:
        return parse_rss(_fetch_text(RSS_URL, timeout=20), lookback_days)
    except Exception as exc:
        logger.error("war.gov contracts RSS fetch failed: %s", exc)
        raise


_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(\d{4})\b",
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
# The one-letter suffix must stand alone: "$39,933,725 modification" is not
# $39.9 trillion (the bare [mM] used to swallow the "m" of "modification").
_AWARD_AMOUNT_LINE = re.compile(
    r"\$[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|[mMbBkK](?![A-Za-z])))?",
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
    r"\b([A-Z]{1,2}\d{2,5}[A-Z]{0,2}-\d{2}-[A-Z]-[A-Z]?\d{3,5}"
    r"|[A-Z]{3,6}\d?-\d{2}-[A-Z]-[A-Z]?\d{3,5})\b"
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


_AWARD_VERB_RE = re.compile(r"\b(?:is\s+being|has\s+been|was)\s+awarded\b", re.I)


def _article_body(soup: Any) -> Any:
    """The container holding the awards.

    The first div whose class merely contains "content" is often a header
    banner (war.gov: ``header_banner_content``), so pick the candidate holding
    the most award sentences, and of those the tightest one.
    """
    cands = soup.find_all("div", class_=re.compile(r"body|content|article", re.I))
    cands += soup.find_all(["article", "main"])
    scored = [(len(_AWARD_VERB_RE.findall(c.get_text(" "))), c) for c in cands]
    scored = [(n, c) for n, c in scored if n]
    if not scored:
        return soup
    best = max(n for n, _ in scored)
    return min((c for n, c in scored if n == best), key=lambda c: len(c.get_text()))


def _parse_article(html: str, source_url: str, article_date: date) -> list[Award]:
    settings = get_settings()
    soup = BeautifulSoup(html, "lxml")

    body = _article_body(soup)
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

    # Identity excludes the URL: the same award arrives from the live article,
    # a saved file or a paste, and must dedupe to one row however it came in.
    source_id = content_hash({
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
            logger.error("DoD drop file not found: %s", file_path)
            return []
        text = p.read_text(encoding="utf-8", errors="replace")
        fallback = date.fromtimestamp(p.stat().st_mtime)
        return parse_drop(text, source_url=str(p), fallback_date=fallback)

    settings = get_settings()
    days = lookback_days if lookback_days is not None else settings.lookback_days

    article_urls = _discover_article_urls(days)
    if not article_urls:
        logger.info("war.gov RSS: no contract articles in the last %d day(s).", days)
        return []

    all_awards: list[Award] = []
    for url, article_date in article_urls:
        all_awards.extend(fetch_article_awards(url, article_date))

    logger.info(
        "war.gov: fetched %d qualifying awards across %d articles",
        len(all_awards), len(article_urls),
    )
    return all_awards


def fetch_article_awards(url: str, article_date: date) -> list[Award]:
    """Fetch one contracts article. Raises ``DefenseSiteBlocked`` on a 403 (no retries)."""
    try:
        text = _fetch_text(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise DefenseSiteBlocked(url, article_date) from exc
        raise
    awards = _parse_article(text, url, article_date)
    logger.info("war.gov %s -> %d awards", url, len(awards))
    return awards


def parse_drop(text: str, source_url: str = "manual-drop", fallback_date: date | None = None) -> list[Award]:
    """Parse a contracts page the operator saved (HTML) or pasted (plain text).

    The article date is read from the page ("Contracts for Oct. 8, 2026") so a
    file processed a day late still carries the right date, which also keeps
    award IDs stable across re-ingests.
    """
    is_html = bool(re.search(r"<(p|div|html|article|body)\b", text[:5000], re.I))
    if is_html:
        title = BeautifulSoup(text, "lxml").find(["title", "h1"])
        head = title.get_text(" ", strip=True) if title else ""
        html = text
    else:
        head = text[:400]
        from html import escape
        html = "<article>" + "".join(
            f"<p>{escape(line.strip())}</p>" for line in text.splitlines() if line.strip()
        ) + "</article>"
    article_date = (_parse_date_from_text(head) or _parse_date_from_text(text[:3000])
                    or fallback_date or date.today())
    awards = _parse_article(html, source_url, article_date)
    logger.info("DoD drop (%s, dated %s): parsed %d awards", "html" if is_html else "text",
                article_date, len(awards))
    return awards
