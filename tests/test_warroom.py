"""War Room page + snapshot API + DoD paste box."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from govaward_alpha import db
from govaward_alpha.dashboard.warroom import SECTORS, _verdict, sector_of

PASTED = """Contracts for Oct. 8, 2026
Raytheon Co., Tucson, Arizona, is being awarded a $95,000,000 firm-fixed-price contract for missile guidance systems. Navy is the contracting activity.
BAE Systems Land & Armaments L.P., Sterling Heights, Michigan, was awarded a $250,000,000 modification for combat vehicle support. Army is the contracting activity.
""" + ("filler line\n" * 20)


@pytest.fixture
def client(tmp_path, monkeypatch, no_network):
    from govaward_alpha.services import pentagon_sniper as ps
    monkeypatch.setattr(ps, "pentagon_sniper", ps.PentagonSniper(drop_dir=tmp_path / "drops"))
    db.init_db()
    from govaward_alpha.dashboard.app import app
    return TestClient(app)


def test_home_is_the_war_room_and_classic_still_works(client):
    assert "GovAward War Room" in client.get("/").text
    assert client.get("/classic").status_code == 200


def test_snapshot_shape(client):
    d = client.get("/api/warroom?probe=0").json()
    assert d["sectors"] == [s for s, _ in SECTORS]
    assert {f["source"] for f in d["feeds"]} == {"defense_daily", "sam_gov", "usaspending"}
    assert all(f["verdict"] in ("LIVE", "STALE", "DOWN", "BLOCKED") for f in d["feeds"])
    assert d["mode"]["live_allowed"] is False
    assert d["seconds_to_drop"] >= 0


def test_paste_box_ingests_and_dedupes(client):
    r = client.post("/api/defense/drop", json={"text": PASTED, "dry_run": True}).json()
    assert r["awards_detected"] == 2 and r["inserted_awards"] == 2
    again = client.post("/api/defense/drop", json={"text": PASTED, "dry_run": True}).json()
    assert again["inserted_awards"] == 0
    snap = client.get("/api/warroom?probe=0&days=4000").json()
    assert {a["awardee_name_raw"] for a in snap["awards"]} >= {"Raytheon Co.", "BAE Systems Land & Armaments L.P."}
    dod = next(f for f in snap["feeds"] if f["source"] == "defense_daily")
    assert dod["last_productive_age_h"] is not None   # the paste recorded a source run


def test_paste_box_rejects_tiny_input(client):
    assert client.post("/api/defense/drop", json={"text": "hi"}).status_code == 400


@pytest.mark.parametrize("agency,sub,expected", [
    ("DEPT OF DEFENSE.DEPT OF THE ARMY.AMC", "", "ARMY"),
    ("Department of Defense", "Navy", "NAVY / USMC"),
    ("Department of Defense", "Missile Defense Agency", "MDA / DARPA / DOD"),
    ("DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.DLA LAND", "", "DLA / LOGISTICS"),
    ("National Aeronautics and Space Administration", "", "SPACE / SCIENCE"),
    ("U.S. Customs and Border Protection", "", "HOMELAND"),
    ("Department of Veterans Affairs", "", "CIVILIAN"),
])
def test_sector_buckets(agency, sub, expected):
    assert SECTORS[sector_of(agency, sub)][0] == expected


def test_unreachable_host_is_down_never_connected():
    f = {"source": "sam_gov", "host": "api.sam.gov", "reachable": False, "newest_award": "2026-10-08"}
    assert _verdict(f, {})[0] == "DOWN"
