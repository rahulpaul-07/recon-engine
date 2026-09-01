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

---

### 2026-09-01 — Grading exposed an entity-identity mismatch, then a real bug.

Built the evaluation harness to grade engine output against the answer key.
First run: `net_arithmetic_error` scored recall 0.00 despite the engine
correctly finding both planted rows.

Not a matcher bug. Tier 0 reports the broken identity against the
**transaction**; the answer key recorded it against the **order**. Same
finding, two different primary keys, so the grader never lined them up. The
arithmetic error is a property of the gateway row, so the key was wrong.
Fixed in the generator.

That took the run to 128/128. Which is where the harness stopped being a
formality.

---

### 2026-09-01 — A perfect score on one seed hid a real defect.

Ran the same evaluation across twelve independently generated batches:
resolution 92.7% +/- 0.4%, accuracy 99.9% +/- 0.2%. Ten seeds scored 100%.
Seeds 5 and 9 did not.

Both failures are the same bug. When a statement line is missing, the
discontinuity is only visible at the row *after* it, so Tier 0 flags that
following row as `missing_bank_row`. But that row is not the problem -- the
gap describes its predecessor. On seeds 5 and 9 the dropped line happened to
land immediately before a messy-narration row and an orphan credit, and the
gap detector claimed them, overwriting their true classification.

A precedence conflict between two detectors with a legitimate claim on the
same row. The fix is for the balance-gap check to emit against the gap itself
rather than against the row that reveals it.

Recorded as a known issue rather than patched in the same sitting, because the
fix touches how the answer key identifies a dropped row and I would rather
change that deliberately than at the end of a session.

The wider point: on seed 42 this bug is invisible. A single run would have
reported 100% and I would have believed it. Variance testing is not a
statistical nicety here -- it is the only reason I know this exists.

---

### 2026-09-01 — Fixed the balance-gap precedence bug.

The gap detector was stamping `missing_bank_row` onto whichever row revealed
the discontinuity, overwriting that row's own classification. The gap does not
belong to that row -- it describes the space before it.

Changed both sides to key it as its own entity, `GAP_BEFORE_<row_id>`. The
generator records the answer key the same way, because the dropped line never
appears in the statement and therefore has no id the engine could reference.

Accuracy across twelve seeds went from 99.9% +/- 0.2% to 100.0% +/- 0.0%, and
the two misclassifications are gone. Resolution rate did not move, which is
what I expected: the bug changed how rows were labelled, not how many were
resolved.

---

### 2026-09-02 - First live agent run: both providers failed, and the chain held.

The first genuine model call in this project failed twice over, for two
unrelated reasons, and the run still completed correctly.

Anthropic returned 400: the key was valid but the account had no credit. A
condition indistinguishable from a working setup until the call is actually
made. The chain demoted it and moved on.

Groq then returned 404: `llama-3.3-70b-versatile` no longer exists. Groq
deprecated it in June 2026 and I had written the default from memory rather
than checking the current catalogue. Demoted as well, and the run reported
"every configured provider failed" with both reasons printed.

Two things worth recording. First, the failover mechanism was exercised by a
real double failure rather than a simulated one, and behaved as designed: no
crash, no record misclassified, both causes surfaced. Second, the second
failure is a class of bug worth naming -- a hardcoded model identifier is a
dependency on a vendor's catalogue at a point in time, and vendors retire
models. Changed the default to the recommended replacement and made it
overridable per provider, so a future retirement is a environment variable
rather than a code change.

The original design would have failed on the first error with no second
attempt. Building the chain the day before turned a dead run into a diagnosis.

---

### 2026-09-02 - The agent disagreed with my answer key, and it was right to.

First full run: 13 exceptions, 13 answered, 45 model calls, no step-limit hits
and no unparseable verdicts. The agent agreed with the deterministic matcher on
12 of 13. The disagreement is the most useful result the project has produced.

BNK000004 is a bank credit of Rs 4,200 that I planted as an orphan -- money with
no corresponding settlement. The matcher classified it that way. The agent did
not.

It searched settlements by amount and found nothing. It ran subset-sum at the
default bound, 3 days and 4 terms, and found nothing. Then it widened the search
itself to 7 days and 8 terms and found five transactions summing to exactly
Rs 4,200.00. It checked balance continuity to confirm the credit was real, and
classified the record `ambiguous_match` rather than `orphan_bank_credit`,
reasoning that the payout may have been batched without a settlement record
being raised.

I measured whether that match was real. Over a 40-transaction pool, a randomly
chosen amount finds an exact subset match 5% of the time at 4 terms. At 8 terms
across the full 134-transaction pool the search space is over 100 million
combinations, and coincidental exact sums become common rather than rare -- my
own measurement script did not terminate at that bound.

So the agent's match was almost certainly coincidental. But the finding is not
that the agent was wrong. Two things are true:

1. **My answer key is weaker than I thought.** I generated the orphan by
   picking a round amount, and with 134 signed transactions available, most
   amounts have *some* subset that sums to them. The record is not cleanly
   orphaned; it is orphaned only under a search bound I chose.

2. **The `max_terms=4` bound is doing more work than I credited it with.** I
   documented it as an evidential judgement -- that many-term matches are weak
   evidence -- and I now have the number: 5% coincidence at 4 terms, effectively
   unbounded above that. The agent overrode the default and produced exactly the
   false positive the default exists to prevent.

The reason this matters: the engine and the answer key were written by the same
person and share the same assumptions, so they agree with each other. The agent
did not share those assumptions, and found the gap. That is the specific
weakness of the 100% classification figure, demonstrated rather than argued.

Left the generator as it is and documented the property, rather than making the
orphan amounts subset-proof. Changing the data to protect the score would be
the wrong direction; the honest version is that the classification depends on a
stated search bound, and here is what happens when that bound is widened.

---

### 2026-09-02 - Two display bugs in the trace output.

Step numbers read `1. 1. 2. 3.` because the counter incremented per model round
while a single round can issue several tool calls in parallel. Semantically
defensible, visually confusing. Now numbering tool calls rather than rounds.

Worth noting the related fact this surfaced: `MAX_STEPS` bounds model rounds,
not tool calls, so an agent issuing parallel calls can make more than five.
The bound is on reasoning turns, which is the intent, but the name understates
what it permits.

Separately, a fully failed run reported "answered by anthropic:claude-sonnet-4-6"
when nothing had answered -- the chain pre-filled its active provider at
construction instead of leaving it empty until a candidate succeeded. A summary
line that names a model which never ran is exactly the kind of quiet
misstatement this project is supposed to avoid.
