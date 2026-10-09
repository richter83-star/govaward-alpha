"""war.gov feed: RSS discovery, 403 handling (no retries), operator drops, cron gate."""

from __future__ import annotations

import urllib.error
from datetime import date, datetime, timezone

import pytest

from govaward_alpha.sources import defense_daily as dd

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Contracts for Oct. 8, 2026</title>
 <link>https://www.war.gov/News/Contracts/Contract/Article/4622898/contracts-for-oct-8-2026/</link>
 <pubDate>Thu, 08 Oct 2026 21:00:19 GMT</pubDate></item>
<item><title>Contracts for Sept. 30, 2026</title>
 <link>https://www.war.gov/News/Contracts/Contract/Article/4610000/contracts-for-sept-30-2026/</link>
 <pubDate>Wed, 30 Sep 2026 21:00:00 GMT</pubDate></item>
<item><title>Something else</title><link>https://www.war.gov/News/Releases/Release/Article/1/</link></item>
</channel></rss>"""

PASTED = """Contracts for Oct. 8, 2026
NAVY
Raytheon Co., Tucson, Arizona, is being awarded a $95,000,000 firm-fixed-price contract for missile guidance systems. Navy is the contracting activity.
Tiny Vendor LLC, Reston, Virginia, is being awarded a $2,000,000 contract for office supplies.
ARMY
BAE Systems Land & Armaments L.P., Sterling Heights, Michigan, was awarded a $250,000,000 modification for combat vehicle support. Army is the contracting activity.
"""


def test_rss_dates_come_from_titles_and_non_articles_are_ignored():
    got = dd.parse_rss(RSS, lookback_days=30, today=date(2026, 10, 9))
    assert [d for _, d in got] == [date(2026, 10, 8), date(2026, 9, 30)]
    assert all("/News/Contracts/Contract/Article/" in u for u, _ in got)


def test_rss_lookback_cutoff():
    got = dd.parse_rss(RSS, lookback_days=1, today=date(2026, 10, 9))
    assert [d for _, d in got] == [date(2026, 10, 8)]


@pytest.mark.parametrize("text,expected", [
    ("Contracts for Oct. 8, 2026", date(2026, 10, 8)),
    ("Contracts for Sept. 30, 2026", date(2026, 9, 30)),
    ("Contracts For September 2, 2026", date(2026, 9, 2)),
])
def test_dod_title_dates(text, expected):
    assert dd._parse_date_from_text(text) == expected


def test_article_403_raises_blocked_without_retry(monkeypatch):
    calls = []

    def boom(url, timeout=25):
        calls.append(url)
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
    monkeypatch.setattr(dd, "_fetch_text", boom)
    with pytest.raises(dd.DefenseSiteBlocked) as ei:
        dd.fetch_article_awards("https://www.war.gov/x", date(2026, 10, 8))
    assert len(calls) == 1 and "pentagon_drops" in str(ei.value)


def test_pasted_text_parses_with_page_date():
    awards = dd.parse_drop(PASTED, fallback_date=date(2000, 1, 1))
    names = sorted(a.awardee_name_raw for a in awards)
    assert names == ["BAE Systems Land & Armaments L.P.", "Raytheon Co."]
    assert {a.posted_date for a in awards} == {date(2026, 10, 8)}
    # the same paste twice yields the same IDs, so a re-ingest is a no-op
    assert {a.source_award_id for a in awards} == {a.source_award_id for a in dd.parse_drop(PASTED)}


def test_header_banner_div_is_not_mistaken_for_the_article_body():
    html = """<html><body>
    <div class="header_banner_content"><p>Department of War $1 billion banner</p></div>
    <div class="body">
      <p>Raytheon, Tucson, Arizona, is being awarded a $95,000,000 contract. Navy is the contracting activity.</p>
      <p>Ernst &amp; Young LLP, New York, New York, is being awarded a contract with a maximum value of $455,430,043.</p>
    </div></body></html>"""
    names = sorted(a.awardee_name_raw for a in dd._parse_article(html, "u", date(2026, 10, 8)))
    assert names == ["Ernst & Young LLP", "Raytheon"]


def test_one_letter_suffix_does_not_eat_the_next_word():
    block = ("Gentex Corp., Simpson, Pennsylvania, was awarded a $39,933,725 modification to "
             "contract W91CRB-24-C-0001 for helmets. Army is the contracting activity.")
    a = dd._parse_block(block, "u", date(2026, 10, 8), 1.0)
    assert a.award_amount == 39_933_725.0


def test_contract_number_with_letter_in_last_group():
    m = dd._CONTRACT_NUMBER_RE.search("on a new contract (HQ0851-27-C-E001). The base value")
    assert m and m.group(1) == "HQ0851-27-C-E001"


def test_real_wargov_page_parses():
    from pathlib import Path
    fx = Path(__file__).parent / "fixtures" / "wargov_contracts_2026-10-08.html"
    if not fx.exists():
        pytest.skip("fixture not present")
    awards = dd.parse_drop(fx.read_text(encoding="utf-8"))
    assert len(awards) >= 3
    assert {a.posted_date for a in awards} == {date(2026, 10, 8)}
    assert any(a.awardee_name_raw.startswith("Raytheon") and a.award_amount > 4e9 for a in awards)
    assert max(a.award_amount for a in awards) < 1e11   # no "$X m(odification)" trillions


@pytest.fixture
def sniper(tmp_path):
    from govaward_alpha.services.pentagon_sniper import PentagonSniper
    return PentagonSniper(drop_dir=tmp_path / "drops")


def test_blocked_article_is_requested_once_then_awaits_a_drop(sniper, monkeypatch):
    url = "https://www.war.gov/News/Contracts/Contract/Article/4622898/x/"
    monkeypatch.setattr(dd, "_discover_article_urls", lambda days: [(url, date(2026, 10, 8))])
    calls = []

    def blocked(u, d):
        calls.append(u)
        raise dd.DefenseSiteBlocked(u, d)
    monkeypatch.setattr(dd, "fetch_article_awards", blocked)
    for _ in range(3):
        res = {}
        assert sniper._fetch_network(1, res) == []
    assert calls == [url]
    assert [d["url"] for d in res["awaiting_drops"]] == [url]


def test_drop_covers_the_blocked_date_and_leaves_the_inbox(sniper, db_conn, no_network, monkeypatch):
    url = "https://www.war.gov/News/Contracts/Contract/Article/4622898/x/"
    sniper._save_state({url: {"status": "blocked", "date": "2026-10-08", "at": "t"}})
    first = sniper.ingest_drop_text(db_conn, PASTED, dry_run=True)
    assert first["awards_detected"] == 2 and first["inserted_awards"] == 2
    assert first["awaiting_drops"] == []
    assert list(sniper.drop_dir.glob("*.txt")) == []
    assert len(list((sniper.drop_dir / "processed").glob("*.txt"))) == 1
    again = sniper.ingest_drop_text(db_conn, PASTED, dry_run=True)
    assert again["inserted_awards"] == 0 and again["alpha_plays"] == [] and again["orders_placed"] == []


def test_stale_drop_files_are_ignored(sniper):
    import os
    import time
    p = sniper.drop_dir / "old.html"
    p.write_text("<p>x</p>")
    old = time.time() - 30 * 3600
    os.utime(p, (old, old))
    assert sniper.find_latest_local_drop() is None


@pytest.mark.parametrize("utc,expected", [
    (datetime(2026, 10, 8, 20, 40, tzinfo=timezone.utc), False),   # 4:40 PM EDT
    (datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc), True),     # 5:00 PM EDT
    (datetime(2026, 10, 8, 23, 35, tzinfo=timezone.utc), False),   # 7:35 PM EDT
    (datetime(2026, 10, 10, 21, 0, tzinfo=timezone.utc), False),   # Saturday
])
def test_evening_watch_gate(utc, expected):
    from govaward_alpha.services.pentagon_sniper import in_evening_watch
    assert in_evening_watch(utc) is expected
