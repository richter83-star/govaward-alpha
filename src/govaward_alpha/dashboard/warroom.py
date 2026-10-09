"""War Room: one honest snapshot for the radar dashboard, plus the DoD paste box.

Everything here is read-only except ``POST /api/defense/drop``, which feeds a
pasted contracts page through the normal sniper path (paper trading only;
LIVE stays locked by ``security.live_allowed``).
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from govaward_alpha.config import get_settings
from govaward_alpha.dashboard.security import live_allowed
from govaward_alpha.db import connect

router = APIRouter()
_HERE = Path(__file__).parent

# -- radar sectors: one wedge per buyer family -------------------------------------
SECTORS = [
    ("ARMY", ("ARMY",)),
    ("NAVY / USMC", ("NAVY", "MARINE", "NAVSUP", "NAVFAC", "NAVSEA", "NAVAIR")),
    ("AIR & SPACE", ("AIR FORCE", "SPACE FORCE", "AFLCMC", "SPACE SYSTEMS")),
    ("DLA / LOGISTICS", ("DEFENSE LOGISTICS", "DLA", "TRANSCOM")),
    ("MDA / DARPA / DOD", ("MISSILE DEFENSE", "DARPA", "DEFENSE ADVANCED", "DEPARTMENT OF WAR",
                           "DEPT OF DEFENSE", "DEPARTMENT OF DEFENSE", "DEFENSE HEALTH", "DISA", "WASHINGTON HEADQUARTERS")),
    ("SPACE / SCIENCE", ("NASA", "AERONAUTICS AND SPACE", "ENERGY", "NATIONAL INSTITUTES", "SCIENCE")),
    ("HOMELAND", ("HOMELAND", "CUSTOMS", "IMMIGRATION", "COAST GUARD", "TRANSPORTATION SECURITY",
                  "CITIZENSHIP", "SECRET SERVICE", "FEMA", "JUSTICE")),
    ("CIVILIAN", ()),  # everything else: GSA, VA, HHS, State, Treasury...
]


def sector_of(agency: str, sub_agency: str) -> int:
    """Index into SECTORS. Sub-agency first (more specific), DoD catch-all last among DoD."""
    text = f"{sub_agency or ''} | {agency or ''}".upper()
    for i, (_, keys) in enumerate(SECTORS[:-1]):
        if i == 4:  # MDA / DARPA / generic DoD: only after the service branches had their chance
            continue
        if any(k in text for k in keys):
            return i
    if any(k in text for k in SECTORS[4][1]):
        return 4
    return len(SECTORS) - 1


# -- reachability (background, cached; a dashboard refresh never waits on the network)
_HOSTS = {"defense_daily": "www.war.gov", "sam_gov": "api.sam.gov", "usaspending": "api.usaspending.gov"}
# What to request per host: a real (cheap) API endpoint where the site root is not
# representative - api.usaspending.gov answers on "/" but its API calls time out from here.
_PROBE_PATH = {"api.usaspending.gov": "/api/v2/awards/last_updated/"}
_REACH: dict[str, tuple[float, bool, float]] = {}
_REACH_BUSY: set[str] = set()
_REACH_TTL = 600.0


def _probe(host: str, timeout: float = 10.0) -> None:
    """One small HTTPS request. Any HTTP status (even 403/404) counts as reachable: the
    question is whether this server can talk to it at all. A TCP connect alone is not
    enough - some government WAFs accept the connection and then never answer."""
    import urllib.error
    import urllib.request
    t0 = time.perf_counter()
    path = _PROBE_PATH.get(host)
    try:
        req = urllib.request.Request(f"https://{host}{path or '/'}", method="GET" if path else "HEAD",
                                     headers={"User-Agent": "GovAwardAlpha/0.2 (health check)"})
        urllib.request.urlopen(req, timeout=timeout).close()
        ok = True
    except urllib.error.HTTPError:
        ok = True
    except Exception:
        ok = False
    _REACH[host] = (time.time(), ok, round((time.perf_counter() - t0) * 1000, 1))
    _REACH_BUSY.discard(host)


def reachable(host: str) -> tuple[bool | None, float | None]:
    """Last known (ok, ms); kicks off a background re-check when older than 10 minutes.
    (None, None) until the first check finishes."""
    import threading
    hit = _REACH.get(host)
    if (not hit or time.time() - hit[0] > _REACH_TTL) and host not in _REACH_BUSY:
        _REACH_BUSY.add(host)
        threading.Thread(target=_probe, args=(host,), daemon=True).start()
    return (hit[1], hit[2]) if hit else (None, None)


def _age_hours(ts: str | None) -> float | None:
    """Hours since a source_runs timestamp (stored as naive UTC by db.start_run)."""
    if not ts:
        return None
    try:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        return round((now_utc - datetime.fromisoformat(ts[:19])).total_seconds() / 3600, 1)
    except ValueError:
        return None


def feed_health(conn: sqlite3.Connection, probe: bool = True) -> list[dict[str, Any]]:
    """Per source: verdict from facts (last success, last error, newest award, reachability)."""
    out = []
    labels = {"defense_daily": "DoD daily contracts (war.gov)", "sam_gov": "SAM.gov award notices",
              "usaspending": "USAspending"}
    for src, label in labels.items():
        last = conn.execute("SELECT started_at, status, records_seen, error_message FROM source_runs "
                            "WHERE source=? ORDER BY id DESC LIMIT 1", (src,)).fetchone()
        ok_run = conn.execute("SELECT max(started_at) FROM source_runs WHERE source=? AND status='success' "
                              "AND records_seen > 0", (src,)).fetchone()[0]
        newest = conn.execute("SELECT max(posted_date), count(*) FROM awards WHERE source=?", (src,)).fetchone()
        row = {"source": src, "label": label, "host": _HOSTS[src],
               "last_run_at": last[0] if last else None, "last_status": last[1] if last else None,
               "last_seen": last[2] if last else None, "last_error": (last[3] or "")[:200] if last else "",
               "last_productive_run_at": ok_run, "last_productive_age_h": _age_hours(ok_run),
               "newest_award": newest[0], "award_count": newest[1]}
        row["parked"] = src in get_settings().parked
        if probe and not row["parked"]:   # a parked source is not polled at all
            row["reachable"], row["connect_ms"] = reachable(_HOSTS[src])
        out.append(row)
    return out


def _verdict(f: dict[str, Any], sniper_state: dict[str, Any]) -> tuple[str, str]:
    """(LIVE | STALE | DOWN | BLOCKED | PARKED, one-line reason). Never 'connected' without evidence."""
    if f.get("parked"):
        return "PARKED", ("off by choice: this UK server can't reach it; history kept. "
                          "Re-enable: remove it from GOVAWARD_PARKED_SOURCES in .env")
    if f.get("reachable") is False:
        return "DOWN", f"{f['host']} does not answer from this server"
    newest = f.get("newest_award")
    age_d = (date.today() - date.fromisoformat(newest)).days if newest else None
    if f["source"] == "defense_daily":
        recent = [v for v in sniper_state.values() if v.get("date", "") >= str(date.today() - timedelta(days=4))]
        if any(v.get("status") == "blocked" for v in recent):
            return "BLOCKED", "war.gov refused today's article - paste it in the DoD drop box"
        if any(v.get("status") == "done" for v in recent):
            n = sum(v.get("awards", 0) or 0 for v in recent if v.get("status") == "done")
            return "LIVE", f"RSS + articles read; {n} qualifying awards in the last 4 days"
    if age_d is None:
        return "STALE", "no awards on file from this source"
    if age_d <= 3:
        return "LIVE", f"newest award {age_d}d old"
    return "STALE", f"newest award {age_d} days old"


def next_drop_et(now_et: datetime) -> datetime:
    """Next weekday 5:00 PM ET (DoD posts contracts ~5pm)."""
    d = now_et.replace(hour=17, minute=0, second=0, microsecond=0)
    if now_et >= d + timedelta(minutes=45):
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


@router.get("/", response_class=HTMLResponse)
def warroom_page() -> HTMLResponse:
    return HTMLResponse((_HERE / "warroom.html").read_text(encoding="utf-8"))


@router.get("/api/warroom")
def warroom(days: int = 75, probe: int = 1) -> dict[str, Any]:
    from govaward_alpha import db
    from govaward_alpha.services.pentagon_sniper import in_evening_watch, pentagon_sniper
    from govaward_alpha.timeutil import get_eastern_time

    conn = connect(get_settings().db_path)
    try:
        state = pentagon_sniper.load_state()
        feeds = feed_health(conn, probe=bool(probe))
        for f in feeds:
            f["verdict"], f["why"] = _verdict(f, state)

        rows = conn.execute("""
            SELECT aw.id, aw.ticker, aw.awardee_name_raw, aw.parent_company_name, aw.award_amount,
                   aw.obligated_amount, aw.agency, aw.sub_agency, aw.posted_date, aw.alert_level,
                   aw.materiality_score, aw.source, aw.source_url, aw.is_idiq, aw.is_modification,
                   aw.is_option_exercise, aw.is_ceiling_only, aw.contract_number, aw.description,
                   co.company_name, co.market_cap, co.price, co.sector,
                   al.id AS alert_id
            FROM awards aw
            LEFT JOIN companies co ON aw.ticker = co.ticker
            LEFT JOIN alerts al ON al.award_id = aw.id
            WHERE aw.posted_date >= date('now', ?)
            ORDER BY aw.posted_date DESC, aw.award_amount DESC
            LIMIT 400
        """, (f"-{int(days)} days",)).fetchall()
        today = date.today()
        awards = []
        for r in rows:
            a = dict(r)
            a["age_days"] = (today - date.fromisoformat(a["posted_date"])).days if a["posted_date"] else None
            amt = a["award_amount"] or a["obligated_amount"] or 0
            cap = a["market_cap"]
            a["ratio_pct"] = round(100.0 * amt / cap, 2) if cap else None
            a["sector"] = sector_of(a["agency"] or "", a["sub_agency"] or "")
            a["description"] = (a["description"] or "")[:600]
            if a["ticker"] in ("", "UNRESOLVED"):
                a["ticker"] = None
            awards.append(a)

        stats = db.get_trader_stats(conn)
        pending = db.get_orders(conn, limit=20, status="PENDING_APPROVAL")
        positions = db.get_positions(conn, status="OPEN")
        closed = db.get_positions(conn, status="CLOSED")[:12]

        edge_path = get_settings().data_dir / "event_study.json"
        edge = json.loads(edge_path.read_text(encoding="utf-8")) if edge_path.exists() else None

        now_et = get_eastern_time()
        nd = next_drop_et(now_et)
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "now_et": now_et.isoformat(timespec="seconds"),
            "next_drop_et": nd.isoformat(timespec="seconds"),
            "seconds_to_drop": int((nd - now_et).total_seconds()),
            "sniper_window": in_evening_watch(),
            "mode": {"engine_mode": stats.get("engine_mode"), "live_allowed": live_allowed()},
            "sectors": [s for s, _ in SECTORS],
            "feeds": feeds,
            "dod": {"articles": sorted(({"url": u, **v} for u, v in state.items()),
                                       key=lambda x: x.get("date", ""), reverse=True)[:6],
                    "awaiting": pentagon_sniper.awaiting_drops()},
            "awards": awards,
            "book": {"stats": stats, "pending": pending, "positions": positions, "closed": closed},
            "edge": edge,
        }
    finally:
        conn.close()


@router.post("/api/defense/drop")
async def defense_drop(request: Request) -> dict[str, Any]:
    """Paste of a war.gov contracts page (text or HTML) -> sniper. Paper only."""
    from govaward_alpha.services.pentagon_sniper import pentagon_sniper

    body = await request.json()
    text = str(body.get("text") or "")
    if len(text) < 200:
        raise HTTPException(status_code=400, detail="Paste the whole contracts page (it looks too short).")
    if len(text) > 3_000_000:
        raise HTTPException(status_code=413, detail="That paste is larger than a contracts page should be.")
    conn = connect(get_settings().db_path)
    try:
        res = pentagon_sniper.ingest_drop_text(conn, text, dry_run=bool(body.get("dry_run")))
    finally:
        conn.close()
    keep = ("awards_detected", "inserted_awards", "tickers_matched", "alpha_plays", "orders_placed",
            "errors", "awaiting_drops")
    return {k: res.get(k) for k in keep}
