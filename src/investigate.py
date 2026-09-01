"""
Investigation runner.

Takes the records the deterministic tiers could not resolve and puts each one
through the resolution agent, then prints the full trace: which tools the agent
chose, what each returned, and how it terminated.

The point of printing every step is that the agent's reasoning has to be
auditable. In a financial system, "the model decided" is not an acceptable
account of why a record was classified a particular way. Every conclusion here
can be traced back to a deterministic tool result.

Run:
    python src/investigate.py --data data
    python src/investigate.py --data data --limit 3     # cheaper while iterating
    python src/investigate.py --data data --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent import ResolutionAgent, build_context  # noqa: E402
from core import paise_to_rupees_str  # noqa: E402
from llm import get_provider  # noqa: E402
from matcher import Engine, load  # noqa: E402


def facts_for(resolution, orders, txns, settlements, bank) -> dict:
    """
    Assemble the known values for a record.

    Deliberately minimal: the agent is given identifiers and raw values, not
    conclusions. If it wants to know whether a settlement exists or an amount
    ties, it has to call a tool. Handing it a summary would let it agree with
    the matcher rather than investigate independently.
    """
    eid, etype = resolution.entity_id, resolution.entity_type

    if etype == "order":
        o = next((x for x in orders if x.order_id == eid), None)
        if o:
            return {"order_amount": paise_to_rupees_str(o.order_amount_paise),
                    "order_datetime": o.order_datetime.isoformat(),
                    "order_status": o.order_status,
                    "payment_method": o.payment_method}

    if etype == "bank_row":
        b = next((x for x in bank if x.bank_txn_id == eid), None)
        if b:
            return {"movement": paise_to_rupees_str(b.movement_paise),
                    "value_date": b.value_date.isoformat(),
                    "description": b.description,
                    "utr_column": b.utr or "(empty)"}

    if etype == "settlement":
        s = next((x for x in settlements if x.settlement_id == eid), None)
        if s:
            return {"total": paise_to_rupees_str(s.total_paise),
                    "capture_date": s.capture_date.isoformat(),
                    "payout_date": s.payout_date.isoformat(),
                    "utr": s.utr or "(none)"}

    if etype == "txn":
        t = next((x for x in txns if x.txn_id == eid), None)
        if t:
            return {"txn_type": t.txn_type,
                    "order_ref": t.order_ref or "(none)",
                    "gross": paise_to_rupees_str(t.gross_amount_paise),
                    "fee": paise_to_rupees_str(t.fee_paise),
                    "gst_on_fee": paise_to_rupees_str(t.gst_on_fee_paise),
                    "net": paise_to_rupees_str(t.net_amount_paise),
                    "status": t.status,
                    "payment_method": t.payment_method}

    return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--limit", type=int, default=0,
                    help="investigate only the first N exceptions")
    ap.add_argument("--json", default="", help="write traces to a JSON file")
    ap.add_argument("--provider", default="", help="force a provider")
    args = ap.parse_args()

    datadir = Path(args.data)
    orders, txns, settlements, bank = load(datadir)
    resolutions = Engine(orders, txns, settlements, bank).run()
    exceptions = [r for r in resolutions if not r.resolved]

    if args.limit:
        exceptions = exceptions[:args.limit]

    provider = get_provider(args.provider or None)
    from tools import InvestigationTools
    tools = InvestigationTools(orders, txns, settlements, bank)
    agent = ResolutionAgent(tools, provider=provider)

    print("=" * 74)
    print(f"EXCEPTION INVESTIGATION  --  {len(exceptions)} records")
    from llm import FallbackChain
    if isinstance(provider, FallbackChain):
        print(f"provider chain: {len(provider.candidates)} provider/model "
              f"candidates, first is "
              f"{provider.candidates[0][0]}:{provider.candidates[0][1]}")
    else:
        print(f"provider: {provider.name}"
              f"{'' if provider.available else '  (unavailable)'}")
    print("=" * 74)
    print()

    results = []
    started = time.perf_counter()

    for i, r in enumerate(exceptions, 1):
        facts = facts_for(r, orders, txns, settlements, bank)
        ctx = build_context(r.entity_id, r.entity_type, r.detail, facts)
        t0 = time.perf_counter()
        out = agent.investigate(r.entity_id, r.entity_type, ctx)
        elapsed = time.perf_counter() - t0

        print(f"[{i}/{len(exceptions)}]  matcher said: {r.classification}")
        print(out.trace())
        print(f"     {out.model_calls} model call(s), {elapsed:.1f}s")
        print()

        results.append({
            "entity_id": out.entity_id,
            "entity_type": out.entity_type,
            "matcher_classification": r.classification,
            "agent_classification": out.classification,
            "agreed": out.classification == r.classification,
            "resolved": out.resolved,
            "terminated": out.terminated,
            "reasoning": out.reasoning,
            "analyst_note": out.analyst_note,
            "model_calls": out.model_calls,
            "seconds": round(elapsed, 2),
            "steps": [asdict(s) for s in out.steps],
        })

    total = time.perf_counter() - started

    print("-" * 74)
    agreed = sum(1 for r in results if r["agreed"])
    term = Counter(r["terminated"] for r in results)
    tools_used = Counter(s["tool"] for r in results for s in r["steps"])

    print(f"  investigated        {len(results)}")
    print(f"  agreed with matcher {agreed}/{len(results)}")
    print(f"  termination         "
          f"{', '.join(f'{k}={v}' for k, v in term.items())}")
    print(f"  total model calls   {sum(r['model_calls'] for r in results)}")
    print(f"  wall clock          {total:.1f}s")
    if tools_used:
        print()
        print("  tools the agent chose to call")
        for name, n in tools_used.most_common():
            print(f"    {name:<34} {n}")
    print("-" * 74)

    if isinstance(provider, FallbackChain):
        if provider.active:
            print(f"  answered by         {provider.active.name}:"
                  f"{provider.active_model}")
        else:
            print("  answered by         nothing - every candidate failed")

    if isinstance(provider, FallbackChain) and provider.events:
        print()
        print("  provider/model failover during this run")
        for ev in provider.events:
            print(f"    {ev}")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\n  traces written to {args.json}")


if __name__ == "__main__":
    main()
