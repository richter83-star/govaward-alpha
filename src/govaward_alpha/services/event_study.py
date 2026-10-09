"""Event study: does a contract award move the awardee's stock?

For every ticker-matched award we take the last close *before the news could
trade* (the posted day's close — DoD posts at ~5 PM ET, after the bell — or the
prior close when a source posts during the day) and measure the stock's return
over the next 1 and 5 trading days minus the benchmark's return over the same
days (market-adjusted abnormal return).

Honesty rules baked in:
* one event per ticker per day (several awards on one day are one news event);
* a ticker's later events inside an earlier event's 5-day window are dropped, so
  windows never overlap and no move is counted twice;
* events without enough trading days after them yet are left out, not guessed;
* 95% CIs use the t distribution, and the summary says "not distinguishable
  from zero" whenever the interval spans zero.

Output: ``data/event_study.json`` (read by the War Room "Edge check" panel).
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

HORIZONS = {"1d": 1, "5d": 5}
_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23,
        12: 2.18, 15: 2.13, 20: 2.09, 25: 2.06, 30: 2.04, 40: 2.02, 60: 2.00, 120: 1.98}


def t95(df: int) -> float:
    if df <= 0:
        return float("nan")
    keys = sorted(_T95)
    for k in keys:
        if df <= k:
            return _T95[k]
    return 1.96


def summarize(xs: list[float]) -> dict[str, Any] | None:
    """Mean, t-based 95% CI, median and hit rate of abnormal returns (in %)."""
    n = len(xs)
    if n == 0:
        return None
    mean = statistics.fmean(xs)
    if n > 1:
        half = t95(n - 1) * statistics.stdev(xs) / math.sqrt(n)
        ci = [round(mean - half, 3), round(mean + half, 3)]
    else:
        ci = [None, None]  # one observation has no interval (and JSON has no infinity)
    return {"n": n, "mean": round(mean, 3), "ci95": ci,
            "median": round(statistics.median(xs), 3), "hit_rate": round(100.0 * sum(x > 0 for x in xs) / n, 1)}


def summarize_clustered(pairs: list[tuple[str, float]]) -> dict[str, Any] | None:
    """Like ``summarize`` but the CI treats each *event date* as one observation.

    Awards cluster: 20 awards posted on one day share that day's sector move, so
    they are not 20 independent tests. Averaging per date first, then taking a t
    interval over dates, keeps one bad week for defense stocks from passing for
    a "significant" award effect.
    """
    if not pairs:
        return None
    by_day: dict[str, list[float]] = {}
    for d, v in pairs:
        by_day.setdefault(d, []).append(v)
    day_means = [statistics.fmean(v) for v in by_day.values()]
    base = summarize([v for _, v in pairs])
    base["mean_events"] = base["mean"]           # plain average over events, for reference
    k = len(day_means)
    m = statistics.fmean(day_means)
    base["mean"] = round(m, 3)                   # headline = average of day-averages (matches the CI)
    if k > 1:
        half = t95(k - 1) * statistics.stdev(day_means) / math.sqrt(k)
        base["ci95"] = [round(m - half, 3), round(m + half, 3)]
    else:
        base["ci95"] = [None, None]
    base["dates"] = k
    return base


def build_events(rows: list[dict[str, Any]], window: int = 5) -> list[dict[str, Any]]:
    """Collapse award rows to one event per (ticker, day); drop overlapping windows per ticker."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if not r.get("ticker") or not r.get("posted_date"):
            continue
        k = (r["ticker"], r["posted_date"])
        e = by_key.setdefault(k, {"ticker": r["ticker"], "date": r["posted_date"], "amount": 0.0, "awards": 0,
                                  "source": r.get("source"), "market_cap": r.get("market_cap"),
                                  "level": r.get("alert_level") or "WATCH"})
        e["amount"] += float(r.get("award_amount") or 0)
        e["awards"] += 1
        rank = {"RED": 3, "ORANGE": 2, "YELLOW": 1}
        if rank.get(r.get("alert_level") or "", 0) > rank.get(e["level"], 0):
            e["level"] = r["alert_level"]
    events = sorted(by_key.values(), key=lambda e: (e["ticker"], e["date"]))
    kept, last_end = [], {}
    for e in events:
        d = date.fromisoformat(e["date"])
        if e["ticker"] in last_end and d <= last_end[e["ticker"]]:
            continue
        kept.append(e)
        last_end[e["ticker"]] = d + timedelta(days=int(window * 7 / 5) + 2)
    for e in kept:
        cap = e.get("market_cap")
        e["ratio_pct"] = round(100.0 * e["amount"] / cap, 3) if cap else None
    return kept


def abnormal_returns(event: dict[str, Any], px: dict[str, list[tuple[date, float]]], bench: str) -> dict[str, float]:
    """{'1d': AR%, '5d': AR%} for horizons with enough data. ``px`` maps ticker -> sorted (day, close)."""
    s, b = px.get(event["ticker"]), px.get(bench)
    if not s or not b:
        return {}
    d0 = date.fromisoformat(event["date"])
    # DoD posts after the close, so that day's close is still pre-news. Other sources
    # post during the day: use the prior close.
    after_close = event.get("source") == "defense_daily"
    base_idx = max((i for i, (d, _) in enumerate(s) if (d <= d0 if after_close else d < d0)), default=None)
    if base_idx is None:
        return {}
    bdates = {d: c for d, c in b}
    out = {}
    for name, h in HORIZONS.items():
        j = base_idx + h
        if j >= len(s):
            continue
        d_base, c_base = s[base_idx]
        d_end, c_end = s[j]
        if d_base not in bdates or d_end not in bdates:
            continue
        r_s = c_end / c_base - 1
        r_b = bdates[d_end] / bdates[d_base] - 1
        out[name] = round(100.0 * (r_s - r_b), 4)
    return out


def fetch_closes(tickers: list[str], start: date, end: date) -> dict[str, list[tuple[date, float]]]:
    """Daily closes via yfinance (already a project dependency)."""
    import yfinance as yf

    out: dict[str, list[tuple[date, float]]] = {}
    df = yf.download(sorted(set(tickers)), start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                     progress=False, auto_adjust=True, group_by="ticker", threads=True)
    for t in sorted(set(tickers)):
        try:
            col = df[t]["Close"] if len(set(tickers)) > 1 else df["Close"]
            series = [(idx.date(), float(v)) for idx, v in col.dropna().items()]
        except Exception:
            series = []
        if series:
            out[t] = series
    return out


def run(conn, benchmark: str = "ITA", market_bench: str = "SPY") -> dict[str, Any]:
    """Headline: abnormal return vs ITA (US aerospace & defense ETF), CI clustered by event date.

    Nearly every awardee is a defense / government-services name, so against SPY the
    study mostly measures sector swings; SPY is reported alongside for reference.
    """
    rows = [dict(r) for r in conn.execute("""
        SELECT aw.ticker, aw.posted_date, aw.award_amount, aw.source, aw.alert_level, co.market_cap
        FROM awards aw LEFT JOIN companies co ON co.ticker = aw.ticker
        WHERE aw.ticker IS NOT NULL AND aw.ticker NOT IN ('', 'UNRESOLVED') AND aw.posted_date IS NOT NULL
    """).fetchall()]
    events = build_events(rows)
    if not events:
        return {"error": "no ticker-matched awards yet"}
    start = min(date.fromisoformat(e["date"]) for e in events) - timedelta(days=10)
    px = fetch_closes([e["ticker"] for e in events] + [benchmark, market_bench], start, date.today())

    main: dict[str, list[tuple[str, float]]] = {h: [] for h in HORIZONS}
    mkt: dict[str, list[tuple[str, float]]] = {h: [] for h in HORIZONS}
    groups: dict[str, dict[str, list[tuple[str, float]]]] = {}
    detail = []
    for e in events:
        ar = abnormal_returns(e, px, benchmark)
        ar_m = abnormal_returns(e, px, market_bench)
        detail.append(dict(e, ar=ar, ar_vs_market=ar_m, priced=e["ticker"] in px))
        for h, v in ar.items():
            main[h].append((e["date"], v))
            for g in (f"source:{e['source']}",
                      "ratio:>=5% of cap" if (e["ratio_pct"] or 0) >= 5 else "ratio:<5% of cap",
                      f"level:{e['level']}"):
                groups.setdefault(g, {k: [] for k in HORIZONS})[h].append((e["date"], v))
        for h, v in ar_m.items():
            mkt[h].append((e["date"], v))

    horizons = {h: s for h in HORIZONS if (s := summarize_clustered(main[h]))}
    vs_market = {h: s for h in HORIZONS if (s := summarize_clustered(mkt[h]))}
    by_group = {g: {h: s for h, v in hs.items() if (s := summarize_clustered(v))} for g, hs in sorted(groups.items())}

    priced = [d for d in detail if d["ar"]]
    n_ev, n_days = len(priced), len({d["date"] for d in priced})
    sources = sorted({d["source"] for d in priced})
    v5 = horizons.get("5d") or horizons.get("1d")
    if v5:
        lo, hi = v5["ci95"]
        if lo is None:
            verdict, ci_txt = "too few news days for an interval", "no CI yet"
        else:
            verdict = "positive" if lo > 0 else "negative" if hi < 0 else "not distinguishable from zero"
            ci_txt = f"95% CI {lo:+.2f} to {hi:+.2f}, clustered by day"
        summary = (f"{n_ev} priced events on {n_days} distinct days ({', '.join(sources)}). "
                   f"5-day return vs {benchmark}: {v5['mean']:+.2f}% ({ci_txt}) - {verdict}.")
    else:
        summary = "Not enough priced events yet."
    if n_days < 20:
        summary += (f" Only {n_days} independent news days so far: no conclusion either way yet. "
                    "Same-day DoD awards (the sniper's edge) only began flowing on 2026-10-08.")
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "benchmark": benchmark, "market_benchmark": market_bench,
        "events": n_ev, "event_days": n_days, "tickers": len({d["ticker"] for d in priced}),
        "unpriced_tickers": sorted({d["ticker"] for d in detail if not d["priced"]}),
        "horizons": horizons, "vs_market": vs_market, "by_group": by_group,
        "summary": summary,
        "detail": sorted(detail, key=lambda d: d["date"], reverse=True),
    }


def write(result: dict[str, Any]) -> str:
    from govaward_alpha.config import get_settings
    p = get_settings().data_dir / "event_study.json"
    p.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return str(p)
