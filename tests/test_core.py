"""
Tests for the shared primitives: money, fee rules, working-day calendar.

These are the foundations every other module depends on, so they are tested
against properties rather than examples where possible. A test that only
confirms one hand-picked input passes tells you less than one that asserts an
invariant holds across a range.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import (  # noqa: E402
    FEE_RULES, FEE_TOLERANCE_PAISE, GST_RATE, MONEY_MOVING_STATUSES,
    add_working_days, expected_fee, is_working_day, paise_to_rupees_str,
    rupees_to_paise, working_day_window,
)


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

class TestMoneyRepresentation:
    """Money is integer paise. These tests exist because the alternative --
    floating point -- fails silently and only at scale."""

    @pytest.mark.parametrize("rupees,paise", [
        ("450.00", 45000), ("0.01", 1), ("0.00", 0),
        ("1200.50", 120050), ("99999.99", 9999999),
    ])
    def test_conversion_is_exact(self, rupees, paise):
        assert rupees_to_paise(rupees) == paise

    def test_round_trip_preserves_value(self):
        for p in (1, 99, 100, 45000, 9999999):
            assert rupees_to_paise(paise_to_rupees_str(p)) == p

    def test_negative_amounts_survive_round_trip(self):
        # Refunds and chargebacks are negative. If the display helper mangles
        # the sign, every refund in a report is wrong.
        assert paise_to_rupees_str(-45000) == "-450.00"
        assert paise_to_rupees_str(-1) == "-0.01"

    def test_summation_is_exact_where_float_is_not(self):
        """
        The specific failure this representation prevents.

        In float, 0.1 + 0.2 != 0.3. Summing a realistic settlement of 87
        payments and comparing to a bank credit would drift, and every
        settlement would report as a mismatch.
        """
        amounts = [rupees_to_paise("0.1"), rupees_to_paise("0.2")]
        assert sum(amounts) == rupees_to_paise("0.3")

        # And the float version, demonstrating the bug we are avoiding.
        assert 0.1 + 0.2 != 0.3

    def test_large_batch_sums_without_drift(self):
        amounts = [rupees_to_paise("333.33") for _ in range(1000)]
        assert sum(amounts) == rupees_to_paise("333330.00")


# --------------------------------------------------------------------------
# Fee rules
# --------------------------------------------------------------------------

class TestFeeRules:

    def test_upi_is_zero_rated(self):
        fee, gst = expected_fee(rupees_to_paise("450.00"), "upi")
        assert fee == 0 and gst == 0

    def test_netbanking_is_flat_not_proportional(self):
        """A flat fee is the case a hardcoded percentage gets wrong."""
        small, _ = expected_fee(rupees_to_paise("100.00"), "netbanking")
        large, _ = expected_fee(rupees_to_paise("10000.00"), "netbanking")
        assert small == large == 1200

    def test_card_is_proportional(self):
        small, _ = expected_fee(rupees_to_paise("100.00"), "card")
        large, _ = expected_fee(rupees_to_paise("1000.00"), "card")
        assert large == small * 10

    def test_gst_is_computed_on_the_fee_not_the_gross(self):
        gross = rupees_to_paise("1000.00")
        fee, gst = expected_fee(gross, "card")
        assert gst == int(Decimal(fee) * GST_RATE)
        assert gst < fee

    @pytest.mark.parametrize("method", sorted(FEE_RULES))
    def test_no_method_produces_a_negative_fee(self, method):
        for amount in (1, 100, 45000, 10_000_00):
            fee, gst = expected_fee(amount, method)
            assert fee >= 0 and gst >= 0

    def test_unknown_method_raises_rather_than_defaulting(self):
        """
        A silent default would be worse than a crash: an unrecognised method
        would be reconciled against a fee rule that does not apply to it, and
        the mismatch would be attributed to the gateway rather than to us.
        """
        with pytest.raises(KeyError):
            expected_fee(45000, "cryptocurrency")

    def test_tolerance_is_smaller_than_any_real_discrepancy(self):
        """
        The tolerance absorbs independent rounding of fee and GST. It must not
        be large enough to absorb a genuine overcharge; the smallest planted
        fee defect in the generator is Rs 1.50.
        """
        assert FEE_TOLERANCE_PAISE < rupees_to_paise("1.50")


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

class TestWorkingDayCalendar:

    def test_weekend_is_not_a_working_day(self):
        assert not is_working_day(date(2026, 8, 15))   # Saturday
        assert not is_working_day(date(2026, 8, 16))   # Sunday

    def test_friday_capture_settles_monday(self):
        """The case that breaks naive T+1 arithmetic."""
        friday = date(2026, 8, 14)
        assert friday.weekday() == 4
        assert add_working_days(friday, 1) == date(2026, 8, 17)

    def test_holiday_pushes_settlement_further(self):
        # 28 August 2026 is in the holiday set; a 27th capture skips it.
        assert add_working_days(date(2026, 8, 27), 1) == date(2026, 8, 31)

    def test_window_is_inclusive_and_ordered(self):
        lo, hi = working_day_window(date(2026, 8, 10))
        assert lo <= hi
        assert lo == add_working_days(date(2026, 8, 10), 1)
        assert hi == add_working_days(date(2026, 8, 10), 3)

    def test_window_never_precedes_the_capture(self):
        for day in range(1, 29):
            d = date(2026, 8, day)
            lo, _ = working_day_window(d)
            assert lo > d


# --------------------------------------------------------------------------
# Status semantics
# --------------------------------------------------------------------------

class TestStatusSemantics:

    def test_failed_is_not_a_money_moving_status(self):
        """
        The identity gross - fee - gst == net holds only for rows that moved
        money. This filter is why the consistency check does not raise a false
        exception on every failed payment -- which it did before the scope was
        stated.
        """
        assert "failed" not in MONEY_MOVING_STATUSES
        assert "voided" not in MONEY_MOVING_STATUSES
        assert "captured" in MONEY_MOVING_STATUSES
        assert "lost" in MONEY_MOVING_STATUSES     # chargebacks moved money
