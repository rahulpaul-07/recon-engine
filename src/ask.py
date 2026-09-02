"""
Settlement Q&A.

Answers plain-English questions about a reconciled batch:

    "Why is the 21 August payout lower than I expected?"
    "How much did I lose to fees on card payments?"
    "Which orders were paid but never settled?"

The shape is the same as the resolution agent, and for the same reason. A
language model translates the question into a structured query and explains the
result in English. It never computes the answer -- every figure it reports came
from deterministic code operating on the actual data.

Two failure modes this design forecloses:

  * The model inventing a plausible number. It has no access to one; the only
    numbers in its context are tool results.
  * The model answering a question the data cannot support. Each query returns
    what it found, including nothing, and "the data does not contain this" is a
    valid answer the prompt asks for explicitly.

Run:
    python src/ask.py --data data "why is the 21 August payout low?"
    python src/ask.py --data data --demo      # a fixed set of questions
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import paise_to_rupees_str as _rupees  # noqa: E402


def paise_to_rupees_str(paise: int) -> str:
    """
    Money leaves these tools with its currency attached.

    The bare decimal string that core returns is right for internal use and
    wrong at the boundary with a language model: given "1770.20" with no unit,
    the model rendered every figure as US dollars. A unit is part of an amount,
    not decoration.
    """
    return f"INR {_rupees(paise)}"
from llm import FallbackChain, get_provider  # noqa: E402
from matcher import Engine, load  # noqa: E402

MAX_STEPS = 4


# --------------------------------------------------------------------------
# Query surface
# --------------------------------------------------------------------------
# These are aggregate questions rather than the record-level lookups the
# resolution agent uses. A merchant asking "why is this payout low" wants a
# breakdown, not a row.

class QueryTools:
    def __init__(self, orders, txns, settlements, bank, resolutions):
        self.orders = orders
        self.txns = txns
        self.settlements = settlements
        self.bank = bank
        self.resolutions = resolutions
        self.calls: list[str] = []

    def _rec(self, name: str, payload: dict) -> dict:
        self.calls.append(name)
        return payload

    # -- settlements ------------------------------------------------------

    def explain_settlement(self, settlement_id: str) -> dict:
        """Full breakdown of one payout: what it contains and why it nets out."""
        s = next((x for x in self.settlements
                  if x.settlement_id == settlement_id), None)
        if s is None:
            return self._rec("explain_settlement",
                             {"found": False,
                              "detail": f"no settlement {settlement_id}"})

        members = [t for t in self.txns if t.settlement_id == settlement_id]
        by_type: dict[str, list] = defaultdict(list)
        for t in members:
            by_type[t.txn_type].append(t)

        breakdown = {}
        for kind, rows in by_type.items():
            breakdown[kind] = {
                "count": len(rows),
                "gross": paise_to_rupees_str(
                    sum(r.gross_amount_paise for r in rows)),
                "fees": paise_to_rupees_str(
                    sum(r.fee_paise + r.gst_on_fee_paise for r in rows)),
                "net": paise_to_rupees_str(
                    sum(r.net_amount_paise for r in rows)),
            }

        return self._rec("explain_settlement", {
            "found": True,
            "settlement_id": s.settlement_id,
            "capture_date": str(s.capture_date),
            "payout_date": str(s.payout_date),
            "total": paise_to_rupees_str(s.total_paise),
            "member_count": len(members),
            "breakdown_by_type": breakdown,
            "total_fees": paise_to_rupees_str(
                sum(t.fee_paise + t.gst_on_fee_paise for t in members)),
        })

    def list_settlements(self, on_or_after: str = "",
                         on_or_before: str = "") -> dict:
        """Settlements in a date range, by payout date."""
        rows = self.settlements
        try:
            if on_or_after:
                lo = date.fromisoformat(on_or_after)
                rows = [s for s in rows if s.payout_date >= lo]
            if on_or_before:
                hi = date.fromisoformat(on_or_before)
                rows = [s for s in rows if s.payout_date <= hi]
        except ValueError as exc:
            return self._rec("list_settlements",
                             {"error": f"invalid date: {exc}"})

        return self._rec("list_settlements", {
            "count": len(rows),
            "settlements": [
                {"settlement_id": s.settlement_id,
                 "payout_date": str(s.payout_date),
                 "total": paise_to_rupees_str(s.total_paise)}
                for s in sorted(rows, key=lambda x: x.payout_date)[:25]],
        })

    # -- aggregates -------------------------------------------------------

    def fee_summary(self, payment_method: str = "") -> dict:
        """
        Fees paid, by payment method.

        MDR and chargeback penalties are reported separately and never summed
        into one rate. They are different charges with different causes: MDR is
        the cost of accepting a payment, a chargeback penalty is the cost of
        losing a dispute. Combining them produced a 1.85% "effective rate" on
        UPI, which is zero-MDR -- the figure was three chargeback penalties
        divided by an unrelated gross.

        The denominator is payment gross only. Including refunds and
        chargebacks, which are negative, would distort the rate in the opposite
        direction.
        """
        live = [t for t in self.txns
                if t.status in ("captured", "processed", "lost")]
        if payment_method:
            live = [t for t in live if t.payment_method == payment_method]

        payments = [t for t in live if t.txn_type == "payment"]
        penalties = [t for t in live if t.txn_type == "chargeback"]

        by_method: dict[str, dict] = defaultdict(
            lambda: {"count": 0, "gross": 0, "mdr": 0, "penalties": 0})

        for t in payments:
            m = by_method[t.payment_method]
            m["count"] += 1
            m["gross"] += t.gross_amount_paise
            m["mdr"] += t.fee_paise + t.gst_on_fee_paise
        for t in penalties:
            by_method[t.payment_method]["penalties"] += (
                t.fee_paise + t.gst_on_fee_paise)

        return self._rec("fee_summary", {
            "total_mdr": paise_to_rupees_str(
                sum(t.fee_paise + t.gst_on_fee_paise for t in payments)),
            "total_chargeback_penalties": paise_to_rupees_str(
                sum(t.fee_paise + t.gst_on_fee_paise for t in penalties)),
            "payment_count": len(payments),
            "by_method": {
                k: {"payments": v["count"],
                    "gross": paise_to_rupees_str(v["gross"]),
                    "mdr": paise_to_rupees_str(v["mdr"]),
                    "mdr_rate": (f"{v['mdr'] / v['gross'] * 100:.2f}%"
                                 if v["gross"] > 0 else "n/a"),
                    "chargeback_penalties": paise_to_rupees_str(
                        v["penalties"])}
                for k, v in by_method.items()},
            "note": ("MDR and chargeback penalties are separate charges and "
                     "are not combined into a single rate"),
        })

    def refund_summary(self) -> dict:
        rows = [t for t in self.txns
                if t.txn_type in ("refund", "chargeback")]
        by_type = Counter(t.txn_type for t in rows)
        return self._rec("refund_summary", {
            "count": len(rows),
            "by_type": dict(by_type),
            "total_value": paise_to_rupees_str(
                -sum(t.gross_amount_paise for t in rows)),
            "chargeback_penalties": paise_to_rupees_str(
                sum(t.fee_paise + t.gst_on_fee_paise
                    for t in rows if t.txn_type == "chargeback")),
        })

    def exception_summary(self, classification: str = "") -> dict:
        """What the reconciliation could not resolve, and why."""
        rows = [r for r in self.resolutions if not r.resolved]
        if classification:
            rows = [r for r in rows if r.classification == classification]
        return self._rec("exception_summary", {
            "count": len(rows),
            "by_classification": dict(
                Counter(r.classification for r in rows)),
            "records": [{"entity_id": r.entity_id,
                         "classification": r.classification,
                         "detail": r.detail} for r in rows[:20]],
        })

    def reconciliation_summary(self) -> dict:
        resolved = sum(1 for r in self.resolutions if r.resolved)
        return self._rec("reconciliation_summary", {
            "entities": len(self.resolutions),
            "resolved": resolved,
            "resolution_rate": (f"{resolved / len(self.resolutions):.1%}"
                                if self.resolutions else "n/a"),
            "by_classification": dict(
                Counter(r.classification for r in self.resolutions)),
            "by_tier": dict(Counter(r.tier for r in self.resolutions
                                    if r.resolved)),
        })

    def unsettled_value(self) -> dict:
        """Captured but not yet paid out. Correctly unmatched, not an error."""
        rows = [t for t in self.txns
                if t.settlement_id is None
                and t.status in ("captured", "processed")]
        return self._rec("unsettled_value", {
            "count": len(rows),
            "value": paise_to_rupees_str(
                sum(t.net_amount_paise for t in rows)),
            "note": "captured but not yet paid out; expected, not an error",
        })


QUERY_SCHEMA = [
    {"name": "reconciliation_summary",
     "description": "Overall result: how many entities, how many resolved, "
                    "the breakdown by classification and by confidence tier. "
                    "Use first for broad questions.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "explain_settlement",
     "description": "Full breakdown of one payout: member transactions by "
                    "type, gross, fees and net. Use to explain why a specific "
                    "payout is the amount it is.",
     "input_schema": {"type": "object", "properties": {
         "settlement_id": {"type": "string"}}, "required": ["settlement_id"]}},
    {"name": "list_settlements",
     "description": "Settlements within a payout-date range. Use to find a "
                    "settlement id when the question names a date.",
     "input_schema": {"type": "object", "properties": {
         "on_or_after": {"type": "string"},
         "on_or_before": {"type": "string"}}}},
    {"name": "fee_summary",
     "description": "Fees paid, broken down by payment method with the "
                    "effective rate for each. Optionally filtered to one "
                    "method.",
     "input_schema": {"type": "object", "properties": {
         "payment_method": {"type": "string"}}}},
    {"name": "refund_summary",
     "description": "Refunds and chargebacks: counts, total value, and "
                    "chargeback penalty fees.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "exception_summary",
     "description": "Records the reconciliation could not resolve, with "
                    "reasons. Optionally filtered to one classification.",
     "input_schema": {"type": "object", "properties": {
         "classification": {"type": "string"}}}},
    {"name": "unsettled_value",
     "description": "Value captured but not yet paid out.",
     "input_schema": {"type": "object", "properties": {}}},
]

SYSTEM_PROMPT = """You answer questions about a reconciled batch of payment \
records for a merchant.

You have query tools. Every number you report must come from a tool result. Do \
not calculate, estimate, or infer figures yourself, and do not report a number \
that no tool returned.

You have at most {max_steps} tool calls.

Answer in at most four sentences of continuous prose. Do not use markdown, \
bullet points, tables, or bold. Cite the specific figures the tools returned \
inside the sentences.

All amounts are Indian rupees and are returned prefixed with INR. Report them \
that way.

If the data does not contain what the question asks for, say so plainly. "The \
batch does not include that" is a correct and useful answer. An invented figure \
is not.

If answering would require combining figures from different tool results, say \
which figures you would combine and let the reader do it. Do not perform the \
arithmetic yourself."""


@dataclass
class Answer:
    question: str
    text: str = ""
    tools_used: list[str] = field(default_factory=list)
    model_calls: int = 0
    provider: str = ""
    error: str = ""


class QAAgent:
    def __init__(self, tools: QueryTools, provider=None,
                 max_steps: int = MAX_STEPS):
        self.tools = tools
        self.provider = provider or get_provider()
        self.max_steps = max_steps
        self.dispatch = {
            "reconciliation_summary": tools.reconciliation_summary,
            "explain_settlement": tools.explain_settlement,
            "list_settlements": tools.list_settlements,
            "fee_summary": tools.fee_summary,
            "refund_summary": tools.refund_summary,
            "exception_summary": tools.exception_summary,
            "unsettled_value": tools.unsettled_value,
        }

    def ask(self, question: str) -> Answer:
        ans = Answer(question=question)
        if not self.provider.available:
            ans.error = (f"no model available: "
                         f"{getattr(self.provider, 'reason', 'unconfigured')}")
            return ans

        system = SYSTEM_PROMPT.format(max_steps=self.max_steps)
        messages: list[dict] = [{"role": "user", "content": question}]
        before = len(self.tools.calls)

        for _ in range(self.max_steps):
            resp = self.provider.complete(system, messages,
                                          tools=QUERY_SCHEMA, max_tokens=900)
            ans.model_calls += 1

            if not resp.ok:
                ans.error = resp.error[:160]
                return ans

            if not resp.wants_tools:
                ans.text = resp.text.strip()
                ans.tools_used = self.tools.calls[before:]
                ans.provider = self._provider_label()
                return ans

            messages.append(self.provider.assistant_turn(resp))
            results = []
            for use in resp.tool_calls:
                fn = self.dispatch.get(use.name)
                if fn is None:
                    payload = {"error": f"no such query '{use.name}'"}
                else:
                    try:
                        payload = fn(**use.arguments)
                    except TypeError as exc:
                        payload = {"error": f"invalid arguments: {exc}"}
                results.append((use.id, payload))
            messages.append(self.provider.tool_results_turn(results))

        ans.error = (f"did not conclude within {self.max_steps} tool calls")
        ans.tools_used = self.tools.calls[before:]
        return ans

    def _provider_label(self) -> str:
        p = self.provider
        if isinstance(p, FallbackChain) and p.active:
            return f"{p.active.name}:{p.active_model}"
        return p.name


DEMO_QUESTIONS = [
    "How did the reconciliation go overall?",
    "How much did I pay in fees, and which payment method costs me most?",
    "What could the system not resolve, and how much money is involved?",
    "How much value is captured but not yet paid out?",
    "Did I lose anything to refunds or chargebacks?",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*", help="question to ask")
    ap.add_argument("--data", default="data")
    ap.add_argument("--demo", action="store_true",
                    help="ask a fixed set of questions")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    orders, txns, settlements, bank = load(Path(args.data))
    resolutions = Engine(orders, txns, settlements, bank).run()
    tools = QueryTools(orders, txns, settlements, bank, resolutions)
    agent = QAAgent(tools)

    questions = (DEMO_QUESTIONS if args.demo
                 else [" ".join(args.question)] if args.question
                 else DEMO_QUESTIONS)

    print("=" * 74)
    print("SETTLEMENT Q&A")
    print(f"provider: {agent._provider_label()}")
    print("=" * 74)

    out = []
    for q in questions:
        a = agent.ask(q)
        print()
        print(f"  Q  {a.question}")
        if a.error:
            print(f"  !  {a.error}")
        else:
            for line in _wrap(a.text, 68):
                print(f"  A  {line}" if line is a.text.split("\n")[0]
                      else f"     {line}")
            print(f"     [{', '.join(a.tools_used) or 'no tools'}] "
                  f"{a.model_calls} model call(s)")
        out.append({"question": a.question, "answer": a.text,
                    "tools_used": a.tools_used, "error": a.error})

    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"\n  written to {args.json}")


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


if __name__ == "__main__":
    main()
