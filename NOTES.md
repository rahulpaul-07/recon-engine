# What broke, and what I did about it

Running log. Entries added as things actually break, not reconstructed later.

---

### 2026-08-31 — Verifier flagged 6 "missing" settlements. Five were my checker, not the data.

Wrote a standalone verifier against the generated data before trusting it. It
reported six settlements with no corresponding bank row, which would have meant
the generator was silently dropping payouts.

Five of them were negative settlement totals — days where refunds and
chargebacks exceeded incoming payments. Those *do* appear on the statement, as
**debits**, not credits. My verifier only compared against `credit_paise`, so
every net-negative settlement looked absent.

The fix was in the checker, not the generator: compare against the signed
movement `credit - debit` rather than credits alone.

What this taught me is the thing the reconciliation engine itself has to get
right — **a settlement can be net negative.** If the matcher assumes money only
flows toward the merchant, every refund-heavy day becomes a false exception.
The remaining one genuinely was missing: the row I deliberately drop to test
balance-gap detection.

---

### 2026-08-31 — `gross - fee - gst != net` on 7 rows; I only planted 2.

The gateway row carries a redundant `net_amount_paise` so the engine can verify
the gateway's own arithmetic. I planted 2 rows where that identity is broken.
The verifier found 7.

The other 5 were all `status = failed`. A failed payment reports the *attempted*
gross with `net = 0`, which is what real gateway reports do — no money moved, but
the attempt is still on the report. So the identity legitimately doesn't hold.

Not a data bug — a missing scope rule. The invariant applies only to rows that
actually moved money (`captured` / `processed` / `lost`). Documented in the
generator docstring, and it becomes an explicit filter in the consistency check
rather than an accident.

Cost: about twenty minutes, most of it spent assuming the generator was wrong.
Lesson: when a check fires more often than you planted the defect, suspect the
check.

---

### 2026-08-31 — Refactor dropped a constant the generator still needed.

Extracted money helpers, fee rules and the working-day calendar into `core.py`
so the generator and the engine could not drift apart. Removed the duplicated
block from the generator by slicing between two section headers.

The slice was too wide: `METHOD_WEIGHTS`, which controls how often each payment
method appears, sat inside that range. It is generator-specific — a property of
the simulated merchant, not of the fee rules — so it should never have moved to
`core.py` either. Immediate `NameError` on the next run.

Restored it to the generator with a comment explaining why it belongs there.

Worth recording because the failure was loud and instant, which is the good
case. A slice that had removed something used only on a rare branch would have
survived the run and failed later against a different seed.

---

### 2026-08-31 — Settlement report was missing entirely.

While designing the matcher I found the two ends of the settlement link had no
join between them: gateway rows carry a `settlement_id`, bank rows carry a UTR,
and nothing connected the two. The design assumed a settlement report that the
generator never produced.

Real gateways publish exactly this file, so the fix was to emit
`settlements.csv` with `settlement_id`, capture date, payout date, total and
UTR. Tier 1 now joins bank rows to settlements on UTR; Tier 2 falls back to
amount plus working-day window when the UTR is missing from the narration.

Found by writing the matcher's interface before its implementation.

---

### 2026-08-31 — The verification gate caught something I did not expect it to.

Built Tier 3 to recover settlement references from unstructured bank narration,
with a rule that nothing proposed is trusted until deterministic checks pass:
the reference must resolve to exactly one known settlement, the amount must tie
exactly, and the date must fall inside the plausible payout window.

I built the gate for the language model. It caught a regex result instead.

Two bank rows carry narration like
`ACH C- RAZORPAY SOFT PVT LTD-RZP879978972-COLLECT`. The reference is
well-formed, regex extracts it cleanly, and nothing about the string looks
wrong. Both are planted orphan credits: money on the statement corresponding to
no settlement at all.

Verification rejects them because the reference resolves to zero settlements.
With the gate disabled they are both accepted, fabricating two matches worth
Rs 6,950 and making the books appear to balance while two real breaks vanish.

The lesson I did not expect: the argument for verifying model output is
identical to the argument for verifying regex output. Extraction succeeding is
not the same as the extracted value being correct, and confidence in the
extraction method is irrelevant to that. The gate is not an LLM safety measure.
It is the thing that makes any proposal safe to act on.

Kept `--no-verify` in the code as a deliberate ablation path so this can be
demonstrated rather than asserted.
