"""Safety lock: password on every route, LIVE refused unless the server allows it."""
import base64

import pytest
from fastapi.testclient import TestClient


def _auth(pw):
    return {"Authorization": "Basic " + base64.b64encode(f"bri:{pw}".encode()).decode()}


@pytest.fixture()
def client(monkeypatch):
    # conftest.isolated_settings already points DB_PATH at a temp DB.
    monkeypatch.setenv("GOVAWARD_DASH_PASSWORD", "s3cret-pw")
    monkeypatch.delenv("GOVAWARD_ALLOW_LIVE", raising=False)
    from govaward_alpha import db
    db.init_db()
    from govaward_alpha.dashboard.app import app
    return TestClient(app)


def test_every_route_needs_the_password(client):
    assert client.get("/").status_code == 401
    assert client.get("/api/stats").status_code == 401
    assert client.post("/api/trader/config", json={"engine_mode": "PAPER"}).status_code == 401
    assert client.get("/api/stats", headers=_auth("wrong")).status_code == 401
    assert client.get("/", headers=_auth("s3cret-pw")).status_code == 200


def test_live_switch_refused_without_server_flag(client):
    r = client.post("/api/trader/config", json={"engine_mode": "LIVE"}, headers=_auth("s3cret-pw"))
    assert r.status_code == 403 and "locked" in r.json()["detail"]
    assert client.post("/api/trader/config", json={"engine_mode": "PAPER"},
                       headers=_auth("s3cret-pw")).status_code == 200


def test_live_allowed_only_with_exact_yes(monkeypatch):
    from govaward_alpha.dashboard.security import live_allowed
    for v in ("", "no", "true", "1", "YES please"):
        monkeypatch.setenv("GOVAWARD_ALLOW_LIVE", v)
        assert live_allowed() is False
    monkeypatch.setenv("GOVAWARD_ALLOW_LIVE", "yes")
    assert live_allowed() is True


def test_engine_refuses_live_order_when_locked(monkeypatch):
    from govaward_alpha.services import trade_engine as te
    monkeypatch.delenv("GOVAWARD_ALLOW_LIVE", raising=False)
    order = {"id": 1, "status": "PENDING_APPROVAL", "ticker": "TEST", "total_cost": 100.0,
             "signal_price": 10.0, "mode": "LIVE"}
    monkeypatch.setattr(te, "get_order_by_id", lambda conn, oid: order, raising=False)

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("broker must not be touched when LIVE is locked")
    engine = te.TradeEngine(broker=Boom(), live_broker=Boom())
    assert engine.approve_and_execute_order(conn=None, order_id=1) is None
