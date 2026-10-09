"""Guards for the bad tickers found in the 2026-10-09 approvals audit.

* T-PC: AT&T awards resolved to a *preferred* share instead of the common stock.
* BIOQ: an OTC ("OID") stock trading ~39 shares/day got auto-trade approval.
* MANT / ASGN / T-PC: bulk approval admitted companies with unknown market cap.
"""
from __future__ import annotations

import sys
import types

import pytest

from govaward_alpha import db
from govaward_alpha.enrich import company_resolver as cr
from govaward_alpha.normalize import normalize_company_name


@pytest.fixture
def fake_yf(monkeypatch):
    """Install a stand-in ``yfinance`` module whose Search returns the given quotes."""
    def install(quotes):
        class _Search:
            def __init__(self, *args, **kwargs):
                self.quotes = quotes
        mod = types.ModuleType("yfinance")
        mod.Search = _Search
        monkeypatch.setitem(sys.modules, "yfinance", mod)
    return install


def _guess(raw, fake_yf, quotes):
    fake_yf(quotes)
    return cr._guess_ticker_yfinance(raw, normalize_company_name(raw))


def test_preferred_share_is_rejected(fake_yf):
    q = [{"symbol": "T-PC", "longname": "AT&T Inc.", "quoteType": "EQUITY", "exchange": "NYQ"}]
    assert _guess("AT&T ENTERPRISES, LLC", fake_yf, q) is None


def test_oid_otc_exchange_is_rejected(fake_yf):
    q = [{"symbol": "BIOQ", "longname": "Bioqual, Inc.", "quoteType": "EQUITY", "exchange": "OID"}]
    assert _guess("BIOQUAL, INC.", fake_yf, q) is None


def test_common_stock_still_resolves(fake_yf):
    q = [{"symbol": "AMRC", "longname": "Ameresco, Inc.", "quoteType": "EQUITY", "exchange": "NYQ"}]
    r = _guess("AMERESCO, INC.", fake_yf, q)
    assert r and r["ticker"] == "AMRC"


def test_class_share_is_not_mistaken_for_preferred(fake_yf):
    q = [{"symbol": "BRK-B", "longname": "Berkshire Hathaway Inc.", "quoteType": "EQUITY", "exchange": "NYQ"}]
    r = _guess("BERKSHIRE HATHAWAY INC", fake_yf, q)
    assert r and r["ticker"] == "BRK-B"


def test_att_awardee_maps_to_common_stock():
    assert cr.resolve("AT&T ENTERPRISES, LLC")["ticker"] == "T"


def test_mantech_subsidiary_is_private(monkeypatch):
    monkeypatch.setattr(cr, "_guess_ticker_yfinance", lambda *a, **k: {
        "ticker": "MANT", "parent_company_name": "ManTech", "resolution_method": "yf",
        "confidence": "LOW_CONFIDENCE"})
    assert cr.resolve("MANTECH ADVANCED SYSTEMS INTERNATIONAL, INC.")["ticker"] is None


def test_bulk_approval_skips_unknown_market_cap(db_conn):
    db_conn.execute("INSERT INTO companies (ticker, company_name, market_cap) VALUES "
                    "('GOOD', 'Good Co', 1e9), ('NOCAP', 'No Cap Co', NULL), ('HUGE', 'Huge Co', 5e11)")
    db_conn.commit()
    n = db.bulk_set_company_approvals(db_conn, max_market_cap=1e10)
    approved = {r[0] for r in db_conn.execute("SELECT ticker FROM company_trade_approvals")}
    assert n == 1 and approved == {"GOOD"}
