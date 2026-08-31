"""
Shared primitives: money representation, fee rules, and the working-day
calendar. Imported by both the generator and the reconciliation engine so that
the two cannot drift apart.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------
# Every amount is an integer number of paise. 45000 == Rs 450.00.
# Floats are never used for money: 0.1 + 0.2 != 0.3 under IEEE-754, and
# summing 87 payments to compare against a bank credit would produce spurious
# sub-paise mismatches on every settlement.

def rupees_to_paise(rupees: str | Decimal | int | float) -> int:
    return int((Decimal(str(rupees)) * 100).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def paise_to_rupees_str(paise: int) -> str:
    """Display only. Never used for arithmetic."""
    sign = "-" if paise < 0 else ""
    p = abs(paise)
    return f"{sign}{p // 100}.{p % 100:02d}"


# --------------------------------------------------------------------------
# Fee rules
# --------------------------------------------------------------------------
# MDR is method-dependent. A flat percentage across all methods is the clearest
# sign of a synthetic dataset. UPI has been zero-MDR since January 2020;
# netbanking is typically a flat per-transaction charge.
#
# The zero rate is no longer legally fixed: the Taxation and Other Laws
# (Amendment) Bill, 2026 removed the blanket prohibition in section 10A of the
# Payment and Settlement Systems Act, 2007. A rule table absorbs that change;
# a hardcoded constant would not.

GST_RATE = Decimal("0.18")

FEE_RULES: dict[str, dict] = {
    "card":       {"kind": "percent", "rate": Decimal("0.0200")},
    "wallet":     {"kind": "percent", "rate": Decimal("0.0180")},
    "netbanking": {"kind": "flat",    "amount_paise": 1200},
    "upi":        {"kind": "percent", "rate": Decimal("0.0000")},
}

# Tolerance for fee comparison. Independent rounding of fee and GST on either
# side can legitimately differ by a paise; anything larger is a real mismatch.
FEE_TOLERANCE_PAISE = 2


def expected_fee(gross_paise: int, method: str) -> tuple[int, int]:
    """
    Expected (fee, gst_on_fee) in paise for a gross amount and payment method.

    Rounding: ROUND_HALF_UP applied independently to the fee and to the GST on
    that fee. This is a choice, stated so it can be checked; real systems
    differ, which is a genuine source of one-paise drift.
    """
    rule = FEE_RULES[method]
    if rule["kind"] == "flat":
        fee = Decimal(rule["amount_paise"])
    else:
        fee = Decimal(gross_paise) * rule["rate"]

    fee_paise = int(fee.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    gst_paise = int((Decimal(fee_paise) * GST_RATE).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))
    return fee_paise, gst_paise


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------
# Settlement is T+1 working days. A Friday capture settles Monday, or Tuesday
# if Monday is a holiday. This is what forces the matcher to reason about date
# windows rather than date equality.

HOLIDAYS = {
    date(2026, 8, 15),
    date(2026, 8, 28),
    date(2026, 9, 5),
}


def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def add_working_days(d: date, n: int) -> date:
    current, remaining = d, n
    while remaining > 0:
        current += timedelta(days=1)
        if is_working_day(current):
            remaining -= 1
    return current


def working_day_window(capture: date, min_days: int = 1, max_days: int = 3
                       ) -> tuple[date, date]:
    """
    Plausible payout date range for a capture date. The matcher uses this
    rather than an exact date: a settlement expected on T+1 can legitimately
    appear a day or two later after a holiday or a bank-side delay.
    """
    return add_working_days(capture, min_days), add_working_days(capture, max_days)


# --------------------------------------------------------------------------
# Status semantics
# --------------------------------------------------------------------------
# The identity  gross - fee - gst == net  holds only for rows that actually
# moved money. A failed attempt reports the attempted gross with net = 0,
# which is what real gateway reports do. Checking the identity without this
# filter raises a false exception on every failed payment.

MONEY_MOVING_STATUSES = {"captured", "processed", "lost"}
NON_SETTLING_STATUSES = {"failed", "voided", "authorized"}
