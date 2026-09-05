"""Shared fixtures.

Every test runs against a throwaway DB and cache directory, and the
module-level caches in ``config`` / ``sec_tickers`` / ``company_resolver`` are
reset between tests so one test's settings never leak into the next.
"""

from __future__ import annotations

import pytest

from govaward_alpha import config
from govaward_alpha.enrich import company_resolver
from govaward_alpha.sources import sec_tickers


def _reset_caches() -> None:
    config._settings = None
    sec_tickers._ticker_map = None
    company_resolver._override_cache = None
    company_resolver._watchlist_cache = None


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point the app at a temp DB/cache and clear cached settings."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SAM_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    # Never read the developer's real .env: it would make tests depend on
    # local credentials, and a placeholder SAM_API_KEY there is enough to send
    # an adapter test out to the live API.
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    _reset_caches()
    yield
    _reset_caches()


@pytest.fixture
def db_conn():
    """An initialised database connection against the temp DB."""
    from govaward_alpha import db

    db.init_db()
    with db.get_connection() as conn:
        yield conn


@pytest.fixture
def no_network(monkeypatch):
    """Stop the resolver reaching SEC or yfinance during a test."""
    monkeypatch.setattr(sec_tickers, "get_ticker_map", lambda *a, **k: {})
    monkeypatch.setattr(company_resolver, "get_ticker_map", lambda *a, **k: {})
    monkeypatch.setattr(company_resolver, "_guess_ticker_yfinance", lambda *a, **k: None)
