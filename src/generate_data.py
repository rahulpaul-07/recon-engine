"""
Synthetic data generator for three-way payment reconciliation.

Produces five files:
    ledger.csv        merchant's own order book
    gateway.csv       payment aggregator's transaction report
    settlements.csv   gateway settlement report (links payments to bank credits)
    bank.csv          bank statement lines
    ground_truth.csv  answer key -- NEVER fed to the reconciliation engine

Design rules (see DECISIONS.md):
  * All money is stored as integer paise. Never float.
  * Fees are method-dependent, not a flat percentage.
  * Refunds and chargebacks are NEGATIVE signed amounts.
  * Settlement date is T+1 *working* days, skipping weekends and holidays.
  * Defects are planted deliberately and recorded in the answer key.

Invariant scope note:
    The identity  gross - fee - gst == net  holds ONLY for rows that actually
    moved money (status in captured / processed / lost). A failed attempt
    reports the ATTEMPTED gross with net = 0, which is what real gateway
    reports do. Any consistency check must filter on status first, or it will
    raise a false exception on every failed payment.

Run:  python src/generate_data.py --seed 42 --orders 120
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import (  # noqa: E402
    GST_RATE, add_working_days, expected_fee, paise_to_rupees_str,
    rupees_to_paise,
)

# --------------------------------------------------------------------------
# Data distribution
# --------------------------------------------------------------------------
# How often each payment method appears. This is a property of the simulated
# merchant, not of the fee rules, so it lives here rather than in core.
# UPI-heavy, reflecting current Indian e-commerce mix.

METHOD_WEIGHTS = [("upi", 0.55), ("card", 0.28), ("netbanking", 0.10), ("wallet", 0.07)]


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass
class LedgerRow:
    order_id: str
    order_amount_paise: int
    currency: str
    order_datetime: datetime
    customer_id: str
    order_status: str
    payment_method: str


@dataclass
class GatewayRow:
    txn_id: str
    txn_type: str          # payment | refund | chargeback | chargeback_reversal | adjustment
    order_ref: str | None
    gross_amount_paise: int   # signed
    fee_paise: int
    gst_on_fee_paise: int
    net_amount_paise: int     # signed; redundant by design, lets us verify their arithmetic
    txn_datetime: datetime
    settlement_id: str | None
    payment_method: str
    status: str


@dataclass
class BankRow:
    bank_txn_id: str
    value_date: date
    description: str
    credit_paise: int | None
    debit_paise: int | None
    balance_paise: int
    utr: str | None


@dataclass
class TruthRow:
    entity_id: str
    entity_type: str            # order | txn | bank_row
    expected_classification: str
    expected_match_target: str
    notes: str


# --------------------------------------------------------------------------
# Bank narration templates -- deliberately messy
# --------------------------------------------------------------------------
# Real bank statement narrations are inconsistent, truncated and vary by
# channel. Clean templates expose the UTR; messy ones bury or mangle it.
# This residual is where an LLM legitimately earns its place.

NARRATION_TEMPLATES_CLEAN = [
    "NEFT CR-{bank_code}-RAZORPAY SOFTWARE PVT LTD-{utr}",
    "NEFT-{utr}-RAZORPAY SOFTWARE PRIVATE LIMITED",
    "IMPS/{utr}/RAZORPAY/SETTLEMENT",
]

NARRATION_TEMPLATES_MESSY = [
    "NEFT CR RAZORPAY SOFTWA {utr_partial}",          # truncated UTR
    "TRF FROM RAZORPAY SOFTWARE PVT LTD",             # no UTR at all
    "NEFT-CR-{bank_code}-RZPYSETTL-{utr_spaced}",     # UTR with stray spacing
    "ACH C- RAZORPAY SOFT PVT LTD-{utr}-COLLECT",
]

BANK_CODES = ["HDFC0000123", "ICIC0000456", "UTIB0000789"]


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------

@dataclass
class DefectPlan:
    """How many of each defect to plant. Every one is recorded in the answer key."""
    fee_mismatch: int = 5
    full_refund: int = 6
    partial_refund: int = 4
    chargeback: int = 3
    duplicate_payment: int = 3
    unsettled_tail: int = 6
    failed_payment: int = 5
    orphan_bank_credit: int = 2
    missing_gateway_row: int = 2
    rounding_noise: int = 4
    net_arithmetic_error: int = 2
    missing_bank_row: int = 1


@dataclass
class Generator:
    seed: int = 42
    n_orders: int = 120
    start_date: date = date(2026, 8, 10)
    n_days: int = 12
    opening_balance_paise: int = rupees_to_paise("250000.00")
    plan: DefectPlan = field(default_factory=DefectPlan)

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.ledger: list[LedgerRow] = []
        self.gateway: list[GatewayRow] = []
        self.bank: list[BankRow] = []
        self.truth: list[TruthRow] = []
        self._txn_seq = 0
        self._bank_seq = 0

    # -- id helpers -------------------------------------------------------
    def _next_txn_id(self, prefix: str) -> str:
        self._txn_seq += 1
        return f"{prefix}_{self._txn_seq:06d}"

    def _next_bank_id(self) -> str:
        self._bank_seq += 1
        return f"BNK{self._bank_seq:06d}"

    def _utr(self) -> str:
        return "RZP" + "".join(self.rng.choices("0123456789", k=9))

    def _amount(self) -> int:
        """Realistic order amounts: mostly small, occasional large."""
        if self.rng.random() < 0.85:
            rupees = self.rng.choice([199, 249, 299, 349, 450, 499, 599, 750, 899, 1200])
            rupees += self.rng.choice([0, 0, 0, 0.50, 0.99])
        else:
            rupees = self.rng.choice([2499, 3999, 5750, 8999, 12500])
        return rupees_to_paise(Decimal(str(rupees)))

    def _method(self) -> str:
        r = self.rng.random()
        cum = 0.0
        for m, w in METHOD_WEIGHTS:
            cum += w
            if r <= cum:
                return m
        return "card"

    # -- main -------------------------------------------------------------
    def run(self) -> None:
        self._build_orders_and_payments()
        self._apply_defects()
        self._assign_settlements()
        self._build_bank_statement()

    # -- step 1: clean baseline -------------------------------------------
    def _build_orders_and_payments(self) -> None:
        for i in range(self.n_orders):
            day_offset = self.rng.randrange(self.n_days)
            d = self.start_date + timedelta(days=day_offset)
            ts = datetime(d.year, d.month, d.day,
                          self.rng.randrange(8, 22), self.rng.randrange(60))

            order_id = f"ORD{4000 + i}"
            amount = self._amount()
            method = self._method()

            self.ledger.append(LedgerRow(
                order_id=order_id,
                order_amount_paise=amount,
                currency="INR",
                order_datetime=ts,
                customer_id=f"CUST{self.rng.randrange(1000, 1400)}",
                order_status="paid",
                payment_method=method,
            ))

            fee, gst = expected_fee(amount, method)
            self.gateway.append(GatewayRow(
                txn_id=self._next_txn_id("pay"),
                txn_type="payment",
                order_ref=order_id,
                gross_amount_paise=amount,
                fee_paise=fee,
                gst_on_fee_paise=gst,
                net_amount_paise=amount - fee - gst,
                txn_datetime=ts,
                settlement_id=None,          # assigned later
                payment_method=method,
                status="captured",
            ))
            self._truth(order_id, "order", "clean", "", "matches 1:1 with its payment")

    def _truth(self, eid: str, etype: str, cls: str, target: str, notes: str) -> None:
        # Replace any earlier classification for the same entity.
        self.truth = [t for t in self.truth if t.entity_id != eid]
        self.truth.append(TruthRow(eid, etype, cls, target, notes))

    def _pick_clean_orders(self, n: int, exclude: set[str]) -> list[LedgerRow]:
        pool = [l for l in self.ledger if l.order_id not in exclude]
        return self.rng.sample(pool, min(n, len(pool)))

    def _gw_for(self, order_id: str) -> GatewayRow | None:
        for g in self.gateway:
            if g.order_ref == order_id and g.txn_type == "payment":
                return g
        return None

    # -- step 2: plant defects --------------------------------------------
    def _apply_defects(self) -> None:
        used: set[str] = set()
        p = self.plan

        # (a) fee mismatch -- gateway charged more than the rule says
        for l in self._pick_clean_orders(p.fee_mismatch, used):
            g = self._gw_for(l.order_id)
            inflation = rupees_to_paise(Decimal(str(self.rng.choice([1.50, 2.25, 5.00]))))
            g.fee_paise += inflation
            g.net_amount_paise = g.gross_amount_paise - g.fee_paise - g.gst_on_fee_paise
            used.add(l.order_id)
            self._truth(l.order_id, "order", "fee_mismatch", g.txn_id,
                        f"gateway fee exceeds rule for {l.payment_method} "
                        f"by {paise_to_rupees_str(inflation)}")

        # (b) full refunds -- negative signed rows
        for l in self._pick_clean_orders(p.full_refund, used):
            g = self._gw_for(l.order_id)
            refund_date = l.order_datetime + timedelta(days=self.rng.randrange(1, 4))
            self.gateway.append(GatewayRow(
                txn_id=self._next_txn_id("rfnd"),
                txn_type="refund",
                order_ref=l.order_id,
                gross_amount_paise=-l.order_amount_paise,
                fee_paise=0,               # MDR is typically not returned
                gst_on_fee_paise=0,
                net_amount_paise=-l.order_amount_paise,
                txn_datetime=refund_date,
                settlement_id=None,
                payment_method=l.payment_method,
                status="processed",
            ))
            l.order_status = "refunded"
            g.status = "refunded"
            used.add(l.order_id)
            self._truth(l.order_id, "order", "refund", g.txn_id,
                        "full refund; net settlement contribution is negative")

        # (c) partial refunds
        for l in self._pick_clean_orders(p.partial_refund, used):
            part = (l.order_amount_paise // 2) // 100 * 100   # round to whole rupee
            refund_date = l.order_datetime + timedelta(days=self.rng.randrange(1, 5))
            self.gateway.append(GatewayRow(
                txn_id=self._next_txn_id("rfnd"),
                txn_type="refund",
                order_ref=l.order_id,
                gross_amount_paise=-part,
                fee_paise=0,
                gst_on_fee_paise=0,
                net_amount_paise=-part,
                txn_datetime=refund_date,
                settlement_id=None,
                payment_method=l.payment_method,
                status="processed",
            ))
            used.add(l.order_id)
            self._truth(l.order_id, "order", "partial_refund", "",
                        f"partial refund of {paise_to_rupees_str(part)} "
                        f"against {paise_to_rupees_str(l.order_amount_paise)}")

        # (d) chargebacks -- reversal lands OUT OF PERIOD, with a penalty fee
        for l in self._pick_clean_orders(p.chargeback, used):
            cb_date = l.order_datetime + timedelta(days=self.rng.randrange(18, 30))
            penalty = rupees_to_paise("500.00")
            self.gateway.append(GatewayRow(
                txn_id=self._next_txn_id("cb"),
                txn_type="chargeback",
                order_ref=l.order_id,
                gross_amount_paise=-l.order_amount_paise,
                fee_paise=penalty,
                gst_on_fee_paise=int(Decimal(penalty) * GST_RATE),
                net_amount_paise=-l.order_amount_paise - penalty
                                 - int(Decimal(penalty) * GST_RATE),
                txn_datetime=cb_date,
                settlement_id=None,
                payment_method=l.payment_method,
                status="lost",
            ))
            used.add(l.order_id)
            self._truth(l.order_id, "order", "chargeback", "",
                        "chargeback reverses the sale in a LATER settlement period, "
                        "plus a penalty fee")

        # (e) duplicate payments -- two captures for one order, one voided
        for l in self._pick_clean_orders(p.duplicate_payment, used):
            fee, gst = expected_fee(l.order_amount_paise, l.payment_method)
            self.gateway.append(GatewayRow(
                txn_id=self._next_txn_id("pay"),
                txn_type="payment",
                order_ref=l.order_id,
                gross_amount_paise=l.order_amount_paise,
                fee_paise=fee,
                gst_on_fee_paise=gst,
                net_amount_paise=l.order_amount_paise - fee - gst,
                txn_datetime=l.order_datetime + timedelta(seconds=self.rng.randrange(20, 200)),
                settlement_id=None,
                payment_method=l.payment_method,
                status="voided",          # voided -> must NOT be settled
            ))
            used.add(l.order_id)
            self._truth(l.order_id, "order", "duplicate", "",
                        "two captures exist for one order; the voided one must be "
                        "excluded from settlement")

        # (f) unsettled tail -- captured but settlement_id stays NULL
        tail = sorted(self.ledger, key=lambda r: r.order_datetime, reverse=True)
        tail = [l for l in tail if l.order_id not in used][:p.unsettled_tail]
        for l in tail:
            used.add(l.order_id)
            self._truth(l.order_id, "order", "unsettled", "",
                        "captured near end of window; not yet paid out. "
                        "CORRECTLY unmatched -- not an error")
        self._unsettled_orders = {l.order_id for l in tail}

        # (g) failed payments -- order exists, payment never succeeded
        for l in self._pick_clean_orders(p.failed_payment, used):
            g = self._gw_for(l.order_id)
            g.status = "failed"
            g.fee_paise = 0
            g.gst_on_fee_paise = 0
            g.net_amount_paise = 0
            l.order_status = "failed"
            used.add(l.order_id)
            self._truth(l.order_id, "order", "failed_payment", g.txn_id,
                        "payment attempt failed; no bank credit will ever exist")

        # (h) missing gateway row -- paid order with no gateway record at all
        for l in self._pick_clean_orders(p.missing_gateway_row, used):
            g = self._gw_for(l.order_id)
            self.gateway.remove(g)
            used.add(l.order_id)
            self._truth(l.order_id, "order", "missing_payment", "",
                        "REAL BREAK: order marked paid but no gateway record exists")

        # (i) rounding noise -- one-paise drift, must NOT be flagged as an error
        for l in self._pick_clean_orders(p.rounding_noise, used):
            g = self._gw_for(l.order_id)
            if g is None:
                continue
            g.fee_paise += self.rng.choice([-1, 1])
            g.net_amount_paise = g.gross_amount_paise - g.fee_paise - g.gst_on_fee_paise
            used.add(l.order_id)
            self._truth(l.order_id, "order", "rounding_noise", g.txn_id,
                        "1 paise fee drift from a different rounding rule; "
                        "must be tolerated, not reported")

        # (j) net arithmetic error -- gateway's own net column does not tie
        for l in self._pick_clean_orders(p.net_arithmetic_error, used):
            g = self._gw_for(l.order_id)
            if g is None:
                continue
            g.net_amount_paise += rupees_to_paise("10.00")
            used.add(l.order_id)
            # Recorded against the TRANSACTION, not the order: the broken
            # identity is a property of the gateway row itself, and that is
            # the entity the engine reports it against.
            self._truth(g.txn_id, "txn", "net_arithmetic_error", l.order_id,
                        "gross - fee - gst != net; the gateway's own row is "
                        "internally inconsistent")

    # -- step 3: group payments into settlements ---------------------------
    def _assign_settlements(self) -> None:
        """
        Settlement batching: everything captured on day D that is eligible
        settles together on T+1 working days.
        """
        buckets: dict[date, list[GatewayRow]] = {}
        for g in self.gateway:
            if g.status in ("failed", "voided"):
                continue
            if g.order_ref in getattr(self, "_unsettled_orders", set()):
                continue
            d = g.txn_datetime.date()
            buckets.setdefault(d, []).append(g)

        self.settlements: dict[str, dict] = {}
        for i, (d, rows) in enumerate(sorted(buckets.items())):
            sid = f"setl_{i:04d}"
            pay_date = add_working_days(d, 1)
            total = sum(r.net_amount_paise for r in rows)
            for r in rows:
                r.settlement_id = sid
            self.settlements[sid] = {
                "capture_date": d,
                "payout_date": pay_date,
                "total_paise": total,
                "utr": self._utr(),
                "rows": rows,
            }

    # -- step 4: bank statement -------------------------------------------
    def _build_bank_statement(self) -> None:
        entries: list[tuple[date, str, int, str | None]] = []

        for sid, s in self.settlements.items():
            if s["total_paise"] == 0:
                continue
            entries.append((s["payout_date"], sid, s["total_paise"], s["utr"]))

        # orphan bank credits -- money that matches no settlement
        for _ in range(self.plan.orphan_bank_credit):
            d = self.start_date + timedelta(days=self.rng.randrange(3, self.n_days))
            amt = rupees_to_paise(Decimal(str(self.rng.choice([1500, 2750, 4200]))))
            entries.append((d, "__orphan__", amt, self._utr()))

        entries.sort(key=lambda e: e[0])

        balance = self.opening_balance_paise
        skipped = self.plan.missing_bank_row

        for value_date, sid, amount, utr in entries:
            bank_id = self._next_bank_id()
            balance += amount

            # deliberately drop one row from the statement, but keep the
            # balance moving -- detectable only via the balance column
            if skipped > 0 and sid != "__orphan__" and self.rng.random() < 0.12:
                skipped -= 1
                # Keyed to the gap, not to a row -- the dropped line never
                # appears in the statement, so it has no id the engine could
                # reference. The next row issued reveals the discontinuity.
                self._truth(f"GAP_BEFORE_BNK{self._bank_seq + 1:06d}",
                            "statement_gap", "missing_bank_row", sid,
                            "settlement paid but its statement line is absent; "
                            "detectable only from the running balance gap")
                continue

            messy = self.rng.random() < 0.28
            pool = NARRATION_TEMPLATES_MESSY if messy else NARRATION_TEMPLATES_CLEAN
            template = self.rng.choice(pool)
            desc = template.format(
                bank_code=self.rng.choice(BANK_CODES),
                utr=utr,
                utr_partial=utr[:8],
                utr_spaced=" ".join([utr[:3], utr[3:7], utr[7:]]),
            )
            emitted_utr = utr if (not messy and "{utr}" in template) else None

            self.bank.append(BankRow(
                bank_txn_id=bank_id,
                value_date=value_date,
                description=desc,
                credit_paise=amount if amount > 0 else None,
                debit_paise=-amount if amount < 0 else None,
                balance_paise=balance,
                utr=emitted_utr,
            ))

            if sid == "__orphan__":
                self._truth(bank_id, "bank_row", "orphan_bank_credit", "",
                            "REAL BREAK: bank credit corresponds to no settlement")
            elif messy:
                self._truth(bank_id, "bank_row", "messy_narration", sid,
                            "UTR absent or mangled in narration; must be recovered "
                            "from free text or inferred from amount + date")

    # -- output -----------------------------------------------------------
    def write(self, outdir: Path) -> None:
        outdir.mkdir(parents=True, exist_ok=True)

        with (outdir / "ledger.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["order_id", "order_amount_paise", "currency",
                        "order_datetime", "customer_id", "order_status",
                        "payment_method"])
            for r in sorted(self.ledger, key=lambda x: x.order_datetime):
                w.writerow([r.order_id, r.order_amount_paise, r.currency,
                            r.order_datetime.isoformat(), r.customer_id,
                            r.order_status, r.payment_method])

        with (outdir / "gateway.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["txn_id", "txn_type", "order_ref", "gross_amount_paise",
                        "fee_paise", "gst_on_fee_paise", "net_amount_paise",
                        "txn_datetime", "settlement_id", "payment_method", "status"])
            for r in sorted(self.gateway, key=lambda x: x.txn_datetime):
                w.writerow([r.txn_id, r.txn_type, r.order_ref or "",
                            r.gross_amount_paise, r.fee_paise, r.gst_on_fee_paise,
                            r.net_amount_paise, r.txn_datetime.isoformat(),
                            r.settlement_id or "", r.payment_method, r.status])

        with (outdir / "bank.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["bank_txn_id", "value_date", "description",
                        "credit_paise", "debit_paise", "balance_paise", "utr"])
            for r in self.bank:
                w.writerow([r.bank_txn_id, r.value_date.isoformat(), r.description,
                            r.credit_paise if r.credit_paise is not None else "",
                            r.debit_paise if r.debit_paise is not None else "",
                            r.balance_paise, r.utr or ""])

        with (outdir / "settlements.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["settlement_id", "capture_date", "payout_date",
                        "total_paise", "utr"])
            for sid, s in sorted(self.settlements.items()):
                w.writerow([sid, s["capture_date"].isoformat(),
                            s["payout_date"].isoformat(), s["total_paise"],
                            s["utr"]])

        with (outdir / "ground_truth.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["entity_id", "entity_type", "expected_classification",
                        "expected_match_target", "notes"])
            for r in sorted(self.truth, key=lambda x: x.entity_id):
                w.writerow([r.entity_id, r.entity_type, r.expected_classification,
                            r.expected_match_target, r.notes])

    def summary(self) -> str:
        from collections import Counter
        counts = Counter(t.expected_classification for t in self.truth)
        lines = [
            f"orders        : {len(self.ledger)}",
            f"gateway rows  : {len(self.gateway)}",
            f"bank rows     : {len(self.bank)}",
            f"settlements   : {len(self.settlements)}",
            "",
            "ground truth classification counts:",
        ]
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {k:<24} {v}")
        return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--orders", type=int, default=120)
    ap.add_argument("--out", type=str, default="data")
    args = ap.parse_args()

    gen = Generator(seed=args.seed, n_orders=args.orders)
    gen.run()
    gen.write(Path(args.out))
    print(gen.summary())


if __name__ == "__main__":
    main()
