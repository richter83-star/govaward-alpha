"""Event-study maths: no double counting, correct pre-news base close, honest CIs."""

from __future__ import annotations

from datetime import date

from govaward_alpha.services.event_study import abnormal_returns, build_events, summarize


def _days(start, closes):
    d0 = date.fromisoformat(start)
    out, d = [], d0
    for c in closes:
        while d.weekday() >= 5:
            d = date.fromordinal(d.toordinal() + 1)
        out.append((d, c))
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_one_event_per_ticker_day_and_no_overlapping_windows():
    rows = [
        {"ticker": "KTOS", "posted_date": "2026-09-01", "award_amount": 1e8, "source": "sam_gov", "market_cap": 1e9},
        {"ticker": "KTOS", "posted_date": "2026-09-01", "award_amount": 5e7, "source": "sam_gov", "market_cap": 1e9},
        {"ticker": "KTOS", "posted_date": "2026-09-03", "award_amount": 9e7, "source": "sam_gov", "market_cap": 1e9},
        {"ticker": "KTOS", "posted_date": "2026-09-20", "award_amount": 9e7, "source": "sam_gov", "market_cap": 1e9},
        {"ticker": None, "posted_date": "2026-09-01", "award_amount": 9e9},
    ]
    ev = build_events(rows)
    assert [(e["ticker"], e["date"], e["awards"]) for e in ev] == [("KTOS", "2026-09-01", 2), ("KTOS", "2026-09-20", 1)]
    assert ev[0]["amount"] == 1.5e8 and ev[0]["ratio_pct"] == 15.0


def test_after_close_source_uses_same_day_close_as_base():
    s = _days("2026-09-01", [100, 110, 121, 121, 121, 121, 121])   # Tue 9/1 .. 
    b = _days("2026-09-01", [100] * 7)
    px = {"X": s, "SPY": b}
    dod = abnormal_returns({"ticker": "X", "date": "2026-09-01", "source": "defense_daily"}, px, "SPY")
    sam = abnormal_returns({"ticker": "X", "date": "2026-09-02", "source": "sam_gov"}, px, "SPY")
    assert round(dod["1d"], 6) == 10.0          # base = 9/1 close (news came after the bell)
    assert round(sam["1d"], 6) == 10.0          # base = 9/1 close (prior close for an intraday post)


def test_benchmark_is_subtracted_and_short_windows_are_skipped():
    s = _days("2026-09-01", [100, 102])
    b = _days("2026-09-01", [100, 101])
    ar = abnormal_returns({"ticker": "X", "date": "2026-09-01", "source": "defense_daily"}, {"X": s, "SPY": b}, "SPY")
    assert round(ar["1d"], 6) == 1.0 and "5d" not in ar


def test_clustered_ci_counts_days_not_awards():
    from govaward_alpha.services.event_study import summarize_clustered
    # 20 awards on one bad day look "significant" event-by-event, but they are one observation
    pairs = [("2026-09-02", -3.0 + 0.01 * i) for i in range(20)] + [("2026-09-10", 0.5), ("2026-09-17", 1.0)]
    naive = summarize([v for _, v in pairs])
    clus = summarize_clustered(pairs)
    assert naive["ci95"][1] < 0                       # naive: falsely "negative"
    assert clus["dates"] == 3 and clus["ci95"][0] < 0 < clus["ci95"][1]
    one_day = summarize_clustered([("2026-09-02", 1.0), ("2026-09-02", 2.0)])
    assert one_day["ci95"] == [None, None]


def test_summary_ci_spans_zero_for_noise():
    s = summarize([1.0, -1.0, 0.5, -0.5])
    assert s["n"] == 4 and s["ci95"][0] < 0 < s["ci95"][1]
    assert summarize([]) is None
