"""
Investigation tools available to the resolution agent.

Every tool is deterministic. The agent chooses WHICH tool to call and in what
order; it never computes a result itself and never decides whether a match is
valid. All arithmetic, searching and verification happens here, in code the
model cannot influence.

This is the boundary that makes the agent safe to run against financial data:
the model contributes investigative strategy, the code contributes truth.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import (  # noqa: E402
    FEE_TOLERANCE_PAISE, expected_fee, paise_to_rupees_str, working_day_window,
)


@dataclass
class ToolResult:
    """Structured result. `evidence` is what the agent reasons over."""
    tool: str
    ok: bool
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Subset sum
# --------------------------------------------------------------------------

def subset_sum(values: list[int], target: int, max_terms: int = 4
               ) -> list[int] | None:
    """
    Find indices of a subset summing exactly to target.

    Bounded at max_terms deliberately. The general problem is NP-hard, and an
    unbounded search over a few hundred signed transactions is not tractable.
    The bound is also an evidential decision, not only a performance one: a
    match requiring many terms is weak evidence, because as the number of
    terms grows the chance of a coincidental exact sum rises sharply.

    Signed values are supported, so refunds and chargebacks participate
    naturally rather than needing special handling.
    """
    for k in range(1, max_terms + 1):
        for combo in combinations(range(len(values)), k):
            if sum(values[i] for i in combo) == target:
                return list(combo)
    return None


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------

class InvestigationTools:
    """
    Tool surface for the agent. Each public method is one callable tool.

    Signatures are kept flat (primitives only) so they can be exposed to a
    model as a JSON tool schema without translation.
    """

    def __init__(self, orders, txns, settlements, bank):
        self.orders = orders
        self.txns = txns
        self.settlements = settlements
        self.bank = bank
        self.order_by_id = {o.order_id: o for o in orders}
        self.calls: list[ToolResult] = []

    def _record(self, result: ToolResult) -> ToolResult:
        self.calls.append(result)
        return result

    # -- search -----------------------------------------------------------

    def search_settlements_by_amount(self, amount_paise: int,
                                     tolerance_paise: int = 0) -> ToolResult:
        """Find settlements whose total matches an amount within tolerance."""
        hits = [s for s in self.settlements
                if abs(s.total_paise - amount_paise) <= tolerance_paise]
        return self._record(ToolResult(
            "search_settlements_by_amount", bool(hits),
            f"{len(hits)} settlement(s) totalling "
            f"{paise_to_rupees_str(amount_paise)} (+/- {tolerance_paise}p)",
            {"settlement_ids": [s.settlement_id for s in hits],
             "payout_dates": [str(s.payout_date) for s in hits]}))

    def search_transactions_by_amount(self, amount_paise: int,
                                      tolerance_paise: int = 0) -> ToolResult:
        """Find individual gateway transactions matching an amount."""
        hits = [t for t in self.txns
                if abs(t.gross_amount_paise - amount_paise) <= tolerance_paise]
        return self._record(ToolResult(
            "search_transactions_by_amount", bool(hits),
            f"{len(hits)} transaction(s) near "
            f"{paise_to_rupees_str(amount_paise)}",
            {"txn_ids": [t.txn_id for t in hits[:10]],
             "truncated": len(hits) > 10}))

    # -- composition ------------------------------------------------------

    def find_subset_summing_to(self, target_paise: int, on_date: str,
                               window_days: int = 3,
                               max_terms: int = 4) -> ToolResult:
        """
        Determine which individual transactions compose a given amount.

        Used when a bank credit has no settlement grouping -- the merchant
        received a lump sum and must work out what is inside it.
        """
        try:
            anchor = date.fromisoformat(on_date)
        except ValueError:
            return self._record(ToolResult(
                "find_subset_summing_to", False, f"invalid date '{on_date}'"))

        lo = anchor - timedelta(days=window_days + 2)
        pool = [t for t in self.txns
                if lo <= t.txn_datetime.date() <= anchor
                and t.status in ("captured", "processed", "lost")]

        values = [t.net_amount_paise for t in pool]
        idx = subset_sum(values, target_paise, max_terms=max_terms)

        if idx is None:
            return self._record(ToolResult(
                "find_subset_summing_to", False,
                f"no subset of <= {max_terms} transactions in the "
                f"{window_days}-day window sums to "
                f"{paise_to_rupees_str(target_paise)}",
                {"pool_size": len(pool), "max_terms": max_terms}))

        members = [pool[i] for i in idx]
        return self._record(ToolResult(
            "find_subset_summing_to", True,
            f"{len(members)} transaction(s) sum exactly to "
            f"{paise_to_rupees_str(target_paise)}",
            {"txn_ids": [t.txn_id for t in members],
             "amounts": [paise_to_rupees_str(t.net_amount_paise) for t in members],
             "pool_size": len(pool)}))

    # -- consistency checks -----------------------------------------------

    def check_balance_continuity(self, bank_txn_id: str) -> ToolResult:
        """Detect a statement line missing before the given row."""
        for i, row in enumerate(self.bank):
            if row.bank_txn_id != bank_txn_id:
                continue
            if i == 0:
                return self._record(ToolResult(
                    "check_balance_continuity", True,
                    "first row on statement; no prior balance to compare"))
            prev = self.bank[i - 1]
            expected = prev.balance_paise + row.movement_paise
            gap = row.balance_paise - expected
            return self._record(ToolResult(
                "check_balance_continuity", gap == 0,
                ("balance is continuous" if gap == 0 else
                 f"balance gap of {paise_to_rupees_str(gap)}: a statement "
                 f"line is missing before this row"),
                {"gap_paise": gap}))
        return self._record(ToolResult(
            "check_balance_continuity", False, f"no such row {bank_txn_id}"))

    def check_fee_against_rule(self, txn_id: str) -> ToolResult:
        """Compare a transaction's fee to the rule for its payment method."""
        t = next((x for x in self.txns if x.txn_id == txn_id), None)
        if t is None:
            return self._record(ToolResult(
                "check_fee_against_rule", False, f"no such transaction {txn_id}"))

        exp_fee, exp_gst = expected_fee(t.gross_amount_paise, t.payment_method)
        delta = t.fee_paise - exp_fee
        within = abs(delta) <= FEE_TOLERANCE_PAISE
        return self._record(ToolResult(
            "check_fee_against_rule", within,
            (f"fee matches the {t.payment_method} rule"
             f"{'' if delta == 0 else f' within tolerance ({delta}p drift)'}"
             if within else
             f"fee {paise_to_rupees_str(t.fee_paise)} exceeds the "
             f"{t.payment_method} rule of {paise_to_rupees_str(exp_fee)} "
             f"by {paise_to_rupees_str(delta)}"),
            {"expected_fee_paise": exp_fee, "actual_fee_paise": t.fee_paise,
             "delta_paise": delta, "method": t.payment_method}))

    def check_settlement_composition(self, settlement_id: str) -> ToolResult:
        """Verify a settlement total equals the sum of its member rows."""
        s = next((x for x in self.settlements
                  if x.settlement_id == settlement_id), None)
        if s is None:
            return self._record(ToolResult(
                "check_settlement_composition", False,
                f"no such settlement {settlement_id}"))

        members = [t for t in self.txns if t.settlement_id == settlement_id]
        computed = sum(t.net_amount_paise for t in members)
        ok = computed == s.total_paise
        return self._record(ToolResult(
            "check_settlement_composition", ok,
            (f"{len(members)} members sum to "
             f"{paise_to_rupees_str(computed)}, matching the report"
             if ok else
             f"members sum to {paise_to_rupees_str(computed)} but the report "
             f"states {paise_to_rupees_str(s.total_paise)}"),
            {"member_count": len(members), "computed_paise": computed,
             "reported_paise": s.total_paise,
             "types": sorted({t.txn_type for t in members})}))

    # -- relationship lookups ---------------------------------------------

    def find_related_transactions(self, order_id: str) -> ToolResult:
        """All gateway rows for an order: payments, refunds, chargebacks."""
        rows = [t for t in self.txns if t.order_ref == order_id]
        if not rows:
            return self._record(ToolResult(
                "find_related_transactions", False,
                f"no gateway rows reference {order_id}"))
        return self._record(ToolResult(
            "find_related_transactions", True,
            f"{len(rows)} row(s) reference {order_id}",
            {"rows": [{"txn_id": t.txn_id, "type": t.txn_type,
                       "status": t.status,
                       "gross": paise_to_rupees_str(t.gross_amount_paise),
                       "settlement_id": t.settlement_id or "UNSETTLED"}
                      for t in rows]}))

    def get_order(self, order_id: str) -> ToolResult:
        o = self.order_by_id.get(order_id)
        if o is None:
            return self._record(ToolResult(
                "get_order", False, f"no such order {order_id}"))
        return self._record(ToolResult(
            "get_order", True, f"order {order_id} found",
            {"amount": paise_to_rupees_str(o.order_amount_paise),
             "status": o.order_status, "method": o.payment_method,
             "placed": o.order_datetime.isoformat()}))

    def check_payout_window(self, settlement_id: str,
                            observed_date: str) -> ToolResult:
        """Is an observed bank date plausible for this settlement?"""
        s = next((x for x in self.settlements
                  if x.settlement_id == settlement_id), None)
        if s is None:
            return self._record(ToolResult(
                "check_payout_window", False, f"no such settlement"))
        try:
            observed = date.fromisoformat(observed_date)
        except ValueError:
            return self._record(ToolResult(
                "check_payout_window", False, f"invalid date"))
        lo, hi = working_day_window(s.capture_date)
        ok = lo <= observed <= hi
        return self._record(ToolResult(
            "check_payout_window", ok,
            (f"{observed} is inside the plausible window {lo}..{hi}" if ok else
             f"{observed} is outside the plausible window {lo}..{hi}"),
            {"window_start": str(lo), "window_end": str(hi)}))


# --------------------------------------------------------------------------
# Tool schema for model exposure
# --------------------------------------------------------------------------
# Descriptions are written for the model. Each states what the tool proves,
# not merely what it returns, so the agent can plan rather than guess.

TOOL_SCHEMA = [
    {
        "name": "get_order",
        "description": "Look up a merchant ledger order by id. Use first when "
                       "investigating anything anchored on an order.",
        "input_schema": {"type": "object", "properties": {
            "order_id": {"type": "string"}}, "required": ["order_id"]},
    },
    {
        "name": "find_related_transactions",
        "description": "List every gateway row referencing an order, including "
                       "refunds and chargebacks. Use to explain why a ledger "
                       "amount and a settled amount differ.",
        "input_schema": {"type": "object", "properties": {
            "order_id": {"type": "string"}}, "required": ["order_id"]},
    },
    {
        "name": "search_settlements_by_amount",
        "description": "Find settlements totalling a given amount. Use when a "
                       "bank credit has no usable reference.",
        "input_schema": {"type": "object", "properties": {
            "amount_paise": {"type": "integer"},
            "tolerance_paise": {"type": "integer"}},
            "required": ["amount_paise"]},
    },
    {
        "name": "search_transactions_by_amount",
        "description": "Find individual gateway transactions near an amount. "
                       "Use when a record has no order reference and you need "
                       "to see whether a matching payment exists at all.",
        "input_schema": {"type": "object", "properties": {
            "amount_paise": {"type": "integer"},
            "tolerance_paise": {"type": "integer"}},
            "required": ["amount_paise"]},
    },
    {
        "name": "find_subset_summing_to",
        "description": "Determine which individual transactions compose a lump "
                       "amount. Use when a bank credit matches no single "
                       "settlement and may be an unreported grouping.",
        "input_schema": {"type": "object", "properties": {
            "target_paise": {"type": "integer"},
            "on_date": {"type": "string"},
            "window_days": {"type": "integer"},
            "max_terms": {"type": "integer"}},
            "required": ["target_paise", "on_date"]},
    },
    {
        "name": "check_balance_continuity",
        "description": "Detect whether a statement line is missing before a "
                       "bank row, by checking the running balance. Use when a "
                       "settlement appears to have no bank credit.",
        "input_schema": {"type": "object", "properties": {
            "bank_txn_id": {"type": "string"}}, "required": ["bank_txn_id"]},
    },
    {
        "name": "check_fee_against_rule",
        "description": "Compare a transaction's fee to the rule for its "
                       "payment method. Use to distinguish a real fee "
                       "discrepancy from rounding drift.",
        "input_schema": {"type": "object", "properties": {
            "txn_id": {"type": "string"}}, "required": ["txn_id"]},
    },
    {
        "name": "check_settlement_composition",
        "description": "Verify a settlement total equals the sum of its "
                       "members. Use when a settlement amount looks wrong.",
        "input_schema": {"type": "object", "properties": {
            "settlement_id": {"type": "string"}},
            "required": ["settlement_id"]},
    },
    {
        "name": "check_payout_window",
        "description": "Check whether an observed bank date is plausible for a "
                       "settlement, accounting for working days and holidays.",
        "input_schema": {"type": "object", "properties": {
            "settlement_id": {"type": "string"},
            "observed_date": {"type": "string"}},
            "required": ["settlement_id", "observed_date"]},
    },
]


def build_dispatch(tools: InvestigationTools) -> dict[str, Callable]:
    return {
        "get_order": tools.get_order,
        "find_related_transactions": tools.find_related_transactions,
        "search_settlements_by_amount": tools.search_settlements_by_amount,
        "search_transactions_by_amount": tools.search_transactions_by_amount,
        "find_subset_summing_to": tools.find_subset_summing_to,
        "check_balance_continuity": tools.check_balance_continuity,
        "check_fee_against_rule": tools.check_fee_against_rule,
        "check_settlement_composition": tools.check_settlement_composition,
        "check_payout_window": tools.check_payout_window,
    }
