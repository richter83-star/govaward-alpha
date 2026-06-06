"""Tests for the materiality scoring engine."""

import pytest

from govaward_alpha.models import Award, Company
from govaward_alpha.scoring import LEVEL_ORANGE, LEVEL_RED, LEVEL_WATCH, LEVEL_YELLOW, score_award


def _make_award(**kwargs) -> Award:
    defaults = {
        "source": "test",
        "source_award_id": "TEST-001",
        "awardee_name_raw": "Test Corp",
        "agency": "Department of Defense",
        "description": "Test contract for surveillance drone systems.",
    }
    defaults.update(kwargs)
    return Award(**defaults)


def _make_company(**kwargs) -> Company:
    defaults = {
        "ticker": "TEST",
        "company_name": "Test Corp",
        "market_cap": 500_000_000,    # $500M small-cap
        "trailing_revenue": 200_000_000,
        "avg_volume": 800_000,
        "sector": "Industrials",
    }
    defaults.update(kwargs)
    return Company(**defaults)


class TestScoreAwardBasicRatios:
    def test_large_award_relative_to_mktcap_scores_high(self):
        award = _make_award(award_amount=300_000_000)  # 60% of $500M mktcap
        company = _make_company()
        result = score_award(award, company)
        assert result.score >= 60
        assert result.level in (LEVEL_RED, LEVEL_ORANGE)

    def test_small_award_relative_to_mktcap_scores_low(self):
        # $50M on a $5B market-cap company with high volume — should not reach RED
        award = _make_award(award_amount=50_000_000, description="IT services contract.")
        company = _make_company(
            market_cap=5_000_000_000,   # $5B — not small-cap
            trailing_revenue=3_000_000_000,
            avg_volume=5_000_000,       # high volume — no underfollowed bonus
        )
        result = score_award(award, company)
        assert result.score < 65

    def test_obligated_amount_adds_score(self):
        award = _make_award(award_amount=100_000_000, obligated_amount=60_000_000)
        company = _make_company()
        result_with = score_award(award, company)

        award_no_obl = _make_award(award_amount=100_000_000)
        result_without = score_award(award_no_obl, company)

        assert result_with.score > result_without.score

    def test_revenue_ratio_adds_score(self):
        award = _make_award(award_amount=150_000_000)  # 75% of $200M revenue
        company = _make_company()
        result = score_award(award, company)
        assert any("revenue" in r.lower() for r in result.reasons)


class TestScoreBonuses:
    def test_small_cap_bonus_applied(self):
        award = _make_award(award_amount=50_000_000)
        company = _make_company(market_cap=1_500_000_000)  # < $2B
        result = score_award(award, company)
        assert result.breakdown["bonus_small_cap"] == 10

    def test_no_small_cap_bonus_for_large_cap(self):
        award = _make_award(award_amount=50_000_000)
        company = _make_company(market_cap=5_000_000_000)
        result = score_award(award, company)
        assert result.breakdown["bonus_small_cap"] == 0

    def test_new_award_bonus(self):
        award = _make_award(award_amount=50_000_000, is_modification=False)
        company = _make_company()
        result = score_award(award, company)
        assert result.breakdown["bonus_new_award"] == 10

    def test_low_volume_bonus(self):
        award = _make_award(award_amount=50_000_000)
        company = _make_company(avg_volume=500_000)  # < 1M
        result = score_award(award, company)
        assert result.breakdown["bonus_low_volume"] == 5

    def test_defense_keyword_bonus(self):
        award = _make_award(award_amount=50_000_000, description="Radar surveillance drone ISR contract.")
        company = _make_company()
        result = score_award(award, company)
        assert result.breakdown["bonus_defense_keywords"] == 5


class TestScorePenalties:
    def test_idiq_low_obligation_penalty(self):
        award = _make_award(
            award_amount=200_000_000,
            obligated_amount=1_000_000,  # < 2% of ceiling
            is_idiq=True,
        )
        company = _make_company()
        result = score_award(award, company)
        assert result.breakdown["penalty_idiq_low_obligated"] == -20
        assert any("IDIQ" in r or "ceiling" in r.lower() for r in result.reasons)

    def test_modification_penalty(self):
        award = _make_award(award_amount=100_000_000, is_modification=True)
        company = _make_company()
        result = score_award(award, company)
        assert result.breakdown["penalty_modification"] == -10

    def test_score_clamped_to_100(self):
        award = _make_award(
            award_amount=400_000_000,  # 80% of mktcap — all bonuses fire
            obligated_amount=100_000_000,
        )
        company = _make_company(market_cap=500_000_000, trailing_revenue=200_000_000, avg_volume=500_000)
        result = score_award(award, company)
        assert result.score <= 100

    def test_score_non_negative(self):
        award = _make_award(
            award_amount=50_000_000,
            is_modification=True,
            is_idiq=True,
            obligated_amount=0,
        )
        company = _make_company(market_cap=10_000_000_000)
        result = score_award(award, company)
        assert result.score >= 0


class TestScoreLevels:
    def test_red_level(self):
        award = _make_award(award_amount=400_000_000, obligated_amount=100_000_000)
        company = _make_company(market_cap=500_000_000, trailing_revenue=200_000_000, avg_volume=500_000)
        result = score_award(award, company)
        assert result.level == LEVEL_RED

    def test_watch_level_low_score(self):
        award = _make_award(award_amount=50_000_000)
        company = _make_company(market_cap=8_000_000_000, trailing_revenue=5_000_000_000)
        result = score_award(award, company)
        assert result.level == LEVEL_WATCH

    def test_no_company_still_scores(self):
        award = _make_award(award_amount=100_000_000)
        result = score_award(award, None)
        assert isinstance(result.score, int)
        assert result.score >= 0


class TestScoreReasons:
    def test_reasons_populated(self):
        award = _make_award(award_amount=200_000_000, obligated_amount=50_000_000)
        company = _make_company()
        result = score_award(award, company)
        assert len(result.reasons) > 0
        assert all(isinstance(r, str) for r in result.reasons)
