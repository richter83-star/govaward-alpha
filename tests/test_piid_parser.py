from datetime import date
from govaward_alpha.normalize import detect_flags, extract_piid_fiscal_year


class TestPiidParser:
    def test_extracts_far_4_1603_fiscal_years(self) -> None:
        assert extract_piid_fiscal_year("80JSC017C0006") == 2017
        assert extract_piid_fiscal_year("75N93019C00069") == 2019
        assert extract_piid_fiscal_year("FA8650-19-C-5212") == 2019
        assert extract_piid_fiscal_year("W9124P-26-C-0001") == 2026
        assert extract_piid_fiscal_year("N00019-20-C-0001") == 2020
        assert extract_piid_fiscal_year("HQ0858-22-C-0005") == 2022
        assert extract_piid_fiscal_year("47QTCA19D0001") == 2019

    def test_handles_invalid_or_missing_piid(self) -> None:
        assert extract_piid_fiscal_year("") is None
        assert extract_piid_fiscal_year("SHORT") is None
        assert extract_piid_fiscal_year("NO-DIGITS-HERE") is None

    def test_detect_flags_identifies_legacy_piid(self) -> None:
        flags = detect_flags(
            description="MISSION SYSTEMS OPERATIONS CONTRACT",
            award_type="DEFINITIVE CONTRACT",
            contract_number="80JSC017C0006",
            award_date=date(2026, 9, 3),
        )
        assert flags["is_legacy_contract"] is True
        assert flags["is_modification"] is True

    def test_detect_flags_identifies_legacy_period_start(self) -> None:
        flags = detect_flags(
            description="Animal Care Support",
            award_type="DEFINITIVE CONTRACT",
            contract_number="75N93026C0001",  # 2026 in contract number
            period_start=date(2019, 9, 28),   # but started 7 years ago
            award_date=date(2026, 9, 4),
        )
        assert flags["is_legacy_contract"] is True
        assert flags["is_modification"] is True

    def test_detect_flags_allows_net_new_award(self) -> None:
        flags = detect_flags(
            description="Next Gen Autonomous Drone Interceptor",
            award_type="DEFINITIVE CONTRACT",
            contract_number="W9124P-26-C-0001",
            period_start=date(2026, 9, 1),
            award_date=date(2026, 9, 15),
        )
        assert flags["is_legacy_contract"] is False
        assert flags["is_modification"] is False
