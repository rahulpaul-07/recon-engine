"""
Tiered reconciliation engine.

Matching is not a single operation. It is a cascade of progressively weaker
methods, and every resolution records WHICH tier resolved it. That produces a
confidence-stratified match rate rather than a single opaque percentage.

    Tier 0  Internal consistency   does each source agree with itself?
    Tier 1  Exact key join         order_ref, settlement_id, utr present
    Tier 2  Deterministic inference amount + working-day window + subset sum
    Tier 3  Reference recovery      unstructured bank narration (separate module)
    Tier 4  Unresolved              reported honestly, never silently dropped

Design stance: a later tier may only PROPOSE a link. Acceptance always requires
a deterministic check that the amounts tie. The engine therefore cannot emit a
match that does not balance, regardless of how the candidate was suggested.
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import (  # noqa: E402
    FEE_TOLERANCE_PAISE, MONEY_MOVING_STATUSES, NON_SETTLING_STATUSES,
    expected_fee, paise_to_rupees_str, working_day_window,
)


# --------------------------------------------------------------------------
# Input records
# --------------------------------------------------------------------------

@dataclass
class Order:
    order_id: str
    order_amount_paise: int
    currency: str
    order_datetime: datetime
    customer_id: str
    order_status: str
    payment_method: str


@dataclass
class Txn:
    txn_id: str
    txn_type: str
    order_ref: str | None
    gross_amount_paise: int
    fee_paise: int
    gst_on_fee_paise: int
    net_amount_paise: int
    txn_datetime: datetime
    settlement_id: str | None
    payment_method: str
    status: str


@dataclass
class Settlement:
    settlement_id: str
    capture_date: date
    payout_date: date
    total_paise: int
    utr: str | None


@dataclass
class BankRow:
    bank_txn_id: str
    value_date: date
    description: str
    credit_paise: int | None
    debit_paise: int | None
    balance_paise: int
    utr: str | None

    @property
    def movement_paise(self) -> int:
        """Signed movement. A settlement is net negative on refund-heavy days
        and appears as a debit, so credits alone are not sufficient."""
        return (self.credit_paise or 0) - (self.debit_paise or 0)


# --------------------------------------------------------------------------
# Output records
# --------------------------------------------------------------------------

@dataclass
class Resolution:
    """One entity's outcome. `tier` records how much to trust it."""
    entity_id: str
    entity_type: str          # order | txn | bank_row | settlement
    classification: str
    tier: int
    matched_to: str = ""
    detail: str = ""
    resolved: bool = True


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def _int_or_none(v: str) -> int | None:
    return int(v) if v not in ("", None) else None


def load(datadir: Path) -> tuple[list[Order], list[Txn], list[Settlement], list[BankRow]]:
    with (datadir / "ledger.csv").open() as f:
        orders = [Order(
            order_id=r["order_id"],
            order_amount_paise=int(r["order_amount_paise"]),
            currency=r["currency"],
            order_datetime=datetime.fromisoformat(r["order_datetime"]),
            customer_id=r["customer_id"],
            order_status=r["order_status"],
            payment_method=r["payment_method"],
        ) for r in csv.DictReader(f)]

    with (datadir / "gateway.csv").open() as f:
        txns = [Txn(
            txn_id=r["txn_id"],
            txn_type=r["txn_type"],
            order_ref=r["order_ref"] or None,
            gross_amount_paise=int(r["gross_amount_paise"]),
            fee_paise=int(r["fee_paise"]),
            gst_on_fee_paise=int(r["gst_on_fee_paise"]),
            net_amount_paise=int(r["net_amount_paise"]),
            txn_datetime=datetime.fromisoformat(r["txn_datetime"]),
            settlement_id=r["settlement_id"] or None,
            payment_method=r["payment_method"],
            status=r["status"],
        ) for r in csv.DictReader(f)]

    with (datadir / "settlements.csv").open() as f:
        settlements = [Settlement(
            settlement_id=r["settlement_id"],
            capture_date=date.fromisoformat(r["capture_date"]),
            payout_date=date.fromisoformat(r["payout_date"]),
            total_paise=int(r["total_paise"]),
            utr=r["utr"] or None,
        ) for r in csv.DictReader(f)]

    with (datadir / "bank.csv").open() as f:
        bank = [BankRow(
            bank_txn_id=r["bank_txn_id"],
            value_date=date.fromisoformat(r["value_date"]),
            description=r["description"],
            credit_paise=_int_or_none(r["credit_paise"]),
            debit_paise=_int_or_none(r["debit_paise"]),
            balance_paise=int(r["balance_paise"]),
            utr=r["utr"] or None,
        ) for r in csv.DictReader(f)]

    return orders, txns, settlements, bank


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

@dataclass
class Engine:
    orders: list[Order]
    txns: list[Txn]
    settlements: list[Settlement]
    bank: list[BankRow]

    resolutions: list[Resolution] = field(default_factory=list)

    def __post_init__(self):
        self.txn_by_order: dict[str, list[Txn]] = defaultdict(list)
        for t in self.txns:
            if t.order_ref:
                self.txn_by_order[t.order_ref].append(t)
        self.settlement_by_id = {s.settlement_id: s for s in self.settlements}
        self.txns_by_settlement: dict[str, list[Txn]] = defaultdict(list)
        for t in self.txns:
            if t.settlement_id:
                self.txns_by_settlement[t.settlement_id].append(t)

    def _emit(self, **kw) -> None:
        self.resolutions.append(Resolution(**kw))

    # ---- Tier 0: does each source agree with itself? --------------------
    def tier0_internal_consistency(self) -> None:
        """
        Checks that require only one source.

        Note the status filter: `gross - fee - gst == net` holds only for rows
        that moved money. A failed attempt reports the attempted gross with
        net = 0, so checking it unfiltered raises a false exception on every
        failed payment.
        """
        for t in self.txns:
            if t.status not in MONEY_MOVING_STATUSES:
                continue
            if t.gross_amount_paise - t.fee_paise - t.gst_on_fee_paise != t.net_amount_paise:
                self._emit(
                    entity_id=t.txn_id, entity_type="txn",
                    classification="net_arithmetic_error", tier=0,
                    detail=(f"gross {paise_to_rupees_str(t.gross_amount_paise)} "
                            f"- fee {paise_to_rupees_str(t.fee_paise)} "
                            f"- gst {paise_to_rupees_str(t.gst_on_fee_paise)} "
                            f"!= net {paise_to_rupees_str(t.net_amount_paise)}"),
                    resolved=False)

        # Statement continuity: a gap between consecutive running balances
        # means a line is missing from the statement entirely.
        prev = None
        for row in self.bank:
            if prev is not None:
                expected = prev.balance_paise + row.movement_paise
                if expected != row.balance_paise:
                    gap = row.balance_paise - expected
                    # The gap describes the space BETWEEN two rows, not the
                    # row that reveals it. Emitting it against the row would
                    # overwrite that row's own classification -- which is how
                    # this silently corrupted messy-narration and orphan rows
                    # that happened to follow a dropped line.
                    self._emit(
                        entity_id=f"GAP_BEFORE_{row.bank_txn_id}",
                        entity_type="statement_gap",
                        classification="missing_bank_row", tier=0,
                        detail=(f"balance gap of {paise_to_rupees_str(gap)} "
                                f"before {row.bank_txn_id}: a statement line "
                                f"is absent"),
                        resolved=False)
            prev = row

    # ---- Tier 1: exact key joins ----------------------------------------
    def tier1_exact_keys(self) -> None:
        for o in self.orders:
            txns = self.txn_by_order.get(o.order_id, [])
            payments = [t for t in txns if t.txn_type == "payment"]
            refunds = [t for t in txns if t.txn_type == "refund"]
            chargebacks = [t for t in txns if t.txn_type == "chargeback"]

            if not payments:
                self._emit(
                    entity_id=o.order_id, entity_type="order",
                    classification="missing_payment", tier=1,
                    detail="order present in ledger with no gateway record",
                    resolved=False)
                continue

            live = [t for t in payments if t.status not in NON_SETTLING_STATUSES]
            voided = [t for t in payments if t.status == "voided"]

            if len(payments) > 1:
                self._emit(
                    entity_id=o.order_id, entity_type="order",
                    classification="duplicate", tier=1,
                    matched_to=",".join(t.txn_id for t in payments),
                    detail=(f"{len(payments)} captures for one order; "
                            f"{len(voided)} voided, {len(live)} live"),
                    resolved=len(live) == 1)
                continue

            p = payments[0]

            if p.status == "failed":
                self._emit(
                    entity_id=o.order_id, entity_type="order",
                    classification="failed_payment", tier=1,
                    matched_to=p.txn_id,
                    detail="attempt failed; no settlement will ever exist")
                continue

            if o.payment_method != p.payment_method:
                self._emit(
                    entity_id=o.order_id, entity_type="order",
                    classification="method_mismatch", tier=1,
                    matched_to=p.txn_id,
                    detail=f"ledger says {o.payment_method}, gateway says {p.payment_method}",
                    resolved=False)
                continue

            if o.order_amount_paise != p.gross_amount_paise:
                self._emit(
                    entity_id=o.order_id, entity_type="order",
                    classification="amount_mismatch", tier=1,
                    matched_to=p.txn_id,
                    detail=(f"ledger {paise_to_rupees_str(o.order_amount_paise)} "
                            f"vs gateway {paise_to_rupees_str(p.gross_amount_paise)}"),
                    resolved=False)
                continue

            # Fee validation against the rule table, with tolerance for
            # independent rounding on either side.
            exp_fee, exp_gst = expected_fee(p.gross_amount_paise, p.payment_method)
            fee_delta = p.fee_paise - exp_fee
            if abs(fee_delta) > FEE_TOLERANCE_PAISE:
                self._emit(
                    entity_id=o.order_id, entity_type="order",
                    classification="fee_mismatch", tier=1,
                    matched_to=p.txn_id,
                    detail=(f"fee {paise_to_rupees_str(p.fee_paise)} vs expected "
                            f"{paise_to_rupees_str(exp_fee)} for {p.payment_method} "
                            f"(delta {paise_to_rupees_str(fee_delta)})"),
                    resolved=False)
                continue
            if fee_delta != 0:
                self._emit(
                    entity_id=o.order_id, entity_type="order",
                    classification="rounding_noise", tier=1,
                    matched_to=p.txn_id,
                    detail=f"fee differs by {fee_delta} paise; within tolerance")
                continue

            if chargebacks:
                self._emit(
                    entity_id=o.order_id, entity_type="order",
                    classification="chargeback", tier=1,
                    matched_to=chargebacks[0].txn_id,
                    detail=("sale reversed in a later settlement period, "
                            "with penalty"))
                continue

            if refunds:
                refunded = -sum(r.gross_amount_paise for r in refunds)
                full = refunded == o.order_amount_paise
                self._emit(
                    entity_id=o.order_id, entity_type="order",
                    classification="refund" if full else "partial_refund", tier=1,
                    matched_to=",".join(r.txn_id for r in refunds),
                    detail=(f"{paise_to_rupees_str(refunded)} refunded against "
                            f"{paise_to_rupees_str(o.order_amount_paise)}"))
                continue

            if p.settlement_id is None:
                self._emit(
                    entity_id=o.order_id, entity_type="order",
                    classification="unsettled", tier=1,
                    matched_to=p.txn_id,
                    detail="captured but not yet paid out; correctly unmatched")
                continue

            self._emit(
                entity_id=o.order_id, entity_type="order",
                classification="clean", tier=1,
                matched_to=f"{p.txn_id}|{p.settlement_id}",
                detail="ledger, gateway and settlement agree")

    # ---- Tier 1b: settlement totals tie to their member transactions ----
    def tier1_settlement_totals(self) -> None:
        for s in self.settlements:
            members = self.txns_by_settlement.get(s.settlement_id, [])
            computed = sum(t.net_amount_paise for t in members)
            if computed != s.total_paise:
                self._emit(
                    entity_id=s.settlement_id, entity_type="settlement",
                    classification="settlement_total_mismatch", tier=1,
                    detail=(f"members sum to {paise_to_rupees_str(computed)}, "
                            f"report says {paise_to_rupees_str(s.total_paise)}"),
                    resolved=False)

    # ---- Tier 1c / Tier 2: bank rows to settlements ----------------------
    def match_bank_rows(self) -> None:
        by_utr = {s.utr: s for s in self.settlements if s.utr}
        claimed: set[str] = set()

        deferred: list[BankRow] = []

        # Tier 1: the UTR is present on the statement and known.
        for row in self.bank:
            if row.utr and row.utr in by_utr:
                s = by_utr[row.utr]
                if s.total_paise == row.movement_paise:
                    claimed.add(s.settlement_id)
                    self._emit(
                        entity_id=row.bank_txn_id, entity_type="bank_row",
                        classification="clean", tier=1,
                        matched_to=s.settlement_id,
                        detail="UTR join, amount ties exactly")
                else:
                    self._emit(
                        entity_id=row.bank_txn_id, entity_type="bank_row",
                        classification="amount_mismatch", tier=1,
                        matched_to=s.settlement_id,
                        detail=(f"UTR matches but bank shows "
                                f"{paise_to_rupees_str(row.movement_paise)} "
                                f"vs settlement "
                                f"{paise_to_rupees_str(s.total_paise)}"),
                        resolved=False)
            else:
                deferred.append(row)

        # Tier 2: no usable UTR. Infer from amount and a working-day window.
        # An exact amount match inside the plausible payout window is strong
        # evidence; anything ambiguous is escalated rather than guessed.
        for row in deferred:
            candidates = []
            for s in self.settlements:
                if s.settlement_id in claimed:
                    continue
                if s.total_paise != row.movement_paise:
                    continue
                lo, hi = working_day_window(s.capture_date)
                if lo <= row.value_date <= hi:
                    candidates.append(s)

            if len(candidates) == 1:
                s = candidates[0]
                claimed.add(s.settlement_id)
                self._emit(
                    entity_id=row.bank_txn_id, entity_type="bank_row",
                    classification="clean", tier=2,
                    matched_to=s.settlement_id,
                    detail=("no usable UTR; unique amount match inside the "
                            "T+1..T+3 working-day window"))
            elif len(candidates) > 1:
                self._emit(
                    entity_id=row.bank_txn_id, entity_type="bank_row",
                    classification="ambiguous_match", tier=2,
                    matched_to=",".join(c.settlement_id for c in candidates),
                    detail=(f"{len(candidates)} settlements share this amount "
                            f"and date window; refusing to guess"),
                    resolved=False)
            else:
                self._emit(
                    entity_id=row.bank_txn_id, entity_type="bank_row",
                    classification="orphan_bank_credit", tier=2,
                    detail=(f"{paise_to_rupees_str(row.movement_paise)} on "
                            f"{row.value_date} corresponds to no settlement"),
                    resolved=False)

        # Settlements that were reported but never appeared on the statement.
        for s in self.settlements:
            if s.settlement_id in claimed or s.total_paise == 0:
                continue
            self._emit(
                entity_id=s.settlement_id, entity_type="settlement",
                classification="settlement_not_in_bank", tier=2,
                detail=(f"payout of {paise_to_rupees_str(s.total_paise)} due "
                        f"{s.payout_date} has no statement line"),
                resolved=False)

    # ---- entry point -----------------------------------------------------
    def run(self) -> list[Resolution]:
        self.tier0_internal_consistency()
        self.tier1_exact_keys()
        self.tier1_settlement_totals()
        self.match_bank_rows()
        return self.resolutions


def subset_sum(values: list[int], target: int, max_terms: int = 4
               ) -> list[int] | None:
    """
    Find a subset of `values` summing exactly to `target`.

    Used when a bank credit must be composed from individual payments because
    no settlement grouping is available. Bounded by `max_terms` deliberately:
    an unbounded search over a few hundred transactions is exponential, and a
    match requiring many terms is weak evidence anyway.

    Returns the indices of a matching subset, or None.
    """
    for k in range(1, max_terms + 1):
        for combo in combinations(range(len(values)), k):
            if sum(values[i] for i in combo) == target:
                return list(combo)
    return None


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    args = ap.parse_args()

    orders, txns, settlements, bank = load(Path(args.data))
    engine = Engine(orders, txns, settlements, bank)
    resolutions = engine.run()

    from collections import Counter
    resolved = sum(1 for r in resolutions if r.resolved)
    counts = Counter(r.classification for r in resolutions)
    tiers = Counter(r.tier for r in resolutions if r.resolved)

    print(f"entities examined : {len(resolutions)}")
    print(f"resolved          : {resolved} "
          f"({resolved / len(resolutions) * 100:.1f}%)")
    print(f"unresolved        : {len(resolutions) - resolved}")
    print()
    print("by tier (resolved only):")
    for t in sorted(tiers):
        print(f"  tier {t}: {tiers[t]}")
    print()
    print("by classification:")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<28} {v}")


if __name__ == "__main__":
    main()
