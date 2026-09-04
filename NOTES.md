# What broke, and what I did about it

Running log, written as things broke rather than reconstructed
afterwards. Entries are in the order they happened.

## What is in here

- **2026-08-31** — Verifier flagged 6 "missing" settlements. Five were my checker, not the data.
- **2026-08-31** — `gross - fee - gst != net` on 7 rows; I only planted 2.
- **2026-08-31** — Refactor dropped a constant the generator still needed.
- **2026-08-31** — Settlement report was missing entirely.
- **2026-08-31** — The verification gate caught something I did not expect it to.
- **2026-09-01** — Grading exposed an entity-identity mismatch, then a real bug.
- **2026-09-01** — A perfect score on one seed hid a real defect.
- **2026-09-01** — Fixed the balance-gap precedence bug.
- **2026-09-02** — First live agent run: both providers failed, and the chain held.
- **2026-09-02** — The agent disagreed with my answer key, and it was right to.
- **2026-09-02** — Two display bugs in the trace output.
- **2026-09-02** — The first stress test did not stress anything.
- **2026-09-02** — The Q&A layer reported a 1.85% MDR on zero-MDR UPI.
- **2026-09-02** — The Q&A agent did arithmetic it was told not to, and got it right.
- **2026-09-02** — Every amount was reported in dollars.
- **2026-09-03** — Optimal assignment never beat greedy on real data.

The entries worth reading first, if reading only three:

1. *The verification gate caught something I did not expect it to* —
   the gate was built for the language model and first caught a regex result.
2. *The agent disagreed with my answer key, and it was right to* —
   the agent found a weakness in my own ground truth.
3. *The first stress test did not stress anything* —
   a 100% result that was a sign the experiment was wrong.

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

---

### 2026-09-02 - The first stress test did not stress anything.

Added a defect-density dial and ran the engine from the default rate up to a
batch where every record carries a defect. Accuracy stayed at 100.0% throughout.

That is not a good result, it is a sign the experiment was wrong. Raising
density adds more defects but no new kinds. Each record is classified
independently, so accuracy is invariant to how many defective records exist --
the engine handles exactly the classes it was built for, at any volume. I had
measured something that could not have come out differently.

The genuinely adversarial case is defects that **interact**: two on the same
record. A fee mismatch on an order that is also refunded; a duplicate where one
capture is unsettled. The generator prevented this deliberately, with a `used`
set ensuring one defect per order, because it keeps the answer key unambiguous.
Removing that guard breaks the engine in a specific and informative way:

    compound defects   accuracy
      22% density        100.0%
      37%                 98.6%
      52%                 97.6%
      62%                 95.7%
      82%                 92.3%

The failures are all one shape. ORD4111 carries both a fee mismatch and a
refund. Tier 1 checks fees before refunds, so the engine reports `fee_mismatch`;
the answer key recorded whichever defect was planted last, `refund`. Neither is
wrong. The record genuinely has both conditions.

So the limitation is in the **taxonomy**, not the matcher. A single-label
classification cannot express a record with two simultaneous conditions, and at
high compound density that becomes the dominant error mode. The fix would be to
return a set of classifications per record rather than one, and to grade against
set overlap rather than equality.

Not making that change now. It touches the answer key, the matcher's return
type, the evaluator and the report, and the honest version of this project is
one that states the limitation and shows the curve rather than one that quietly
widens the taxonomy at the end of a build. The measurement is more useful than
the fix would have been.

Worth recording that the first version of this experiment would have gone in the
submission as "100% accuracy even when every record is defective", which is true,
meaningless, and would not have survived one question.

---

### 2026-09-02 - The Q&A layer reported a 1.85% MDR on zero-MDR UPI.

Built aggregate query tools for the Q&A agent and checked each one by calling
it directly, before involving a model. `fee_summary` reported an effective rate
of 1.85% on UPI. UPI is zero-MDR. The figure had to be wrong.

It was summing every fee field on every money-moving row, which folds three
chargeback penalties of Rs 590 each into the same total as merchant discount
rate. UPI's Rs 1,182.27 was Rs 2.27 of real MDR -- from the deliberately
planted fee-mismatch defects -- plus Rs 1,180 of chargeback penalties.

The denominator was wrong too: gross across all row types includes refunds and
chargebacks, which are negative, so the rate was distorted in both directions
at once.

Now MDR and chargeback penalties are reported as separate figures and never
combined into a rate, and the denominator is payment gross only. UPI reads
0.00%, card 2.37%, wallet 2.12%, netbanking 2.82% -- the last being higher
because a flat Rs 12 on small orders is proportionally expensive, which is how
a flat fee behaves.

The lesson is not about fees. It is that an aggregate is a place where two
different things get added together and the result still looks like a number.
The record-level checks in this project all have a natural falsifier -- the
answer key -- and the aggregates do not. This one was caught because UPI has a
rate I knew should be zero. An aggregate over a field I had no prior expectation
for would have shipped wrong.

Worth remembering that the Q&A agent would have reported that 1.85% in fluent
English with a tool result to cite, and it would have been entirely convincing.
Grounding an answer in a tool result guarantees the number came from the data.
It does not guarantee the tool computed the right thing.

---

### 2026-09-02 - The Q&A agent did arithmetic it was told not to, and got it right.

First live run of the Q&A agent. Every figure it reported traced back to a tool
result, except one. Asked about refunds and chargebacks, it answered:

    "between the reversed transactions and chargeback penalties, you lost
     roughly 25,216.49 to these disputes"

No tool returned that. It added the refund total to the penalty total itself.
The sum is correct, which is worse than if it had been wrong -- a wrong number
would have been obvious, and a right one produced by a forbidden route is
indistinguishable from a grounded figure until you go looking.

The cause is mine. The resolution agent's constraints are enforced in code: an
invented classification is downgraded, a resolution with no tool call is
overruled, and both are tested without a model. For the Q&A agent I put "do not
calculate figures yourself" in the prompt and stopped there. A rule in a prompt
is a request, not a constraint, and this is a live example of the distinction in
my own project after arguing the point elsewhere in this file.

Tightened the instruction to require naming the figures rather than combining
them, which is a mitigation, not a fix. The real fix is a tool that returns
combined totals so no addition is ever needed, or a post-check that every number
in the answer appears in a tool result. Recorded rather than built, because the
honest position is that this layer has a weaker guarantee than the resolution
agent and the difference is worth stating.

---

### 2026-09-02 - Every amount was reported in dollars.

The same run rendered every figure as US dollars. The query tools returned bare
decimal strings -- "1770.20" -- and the model supplied a currency symbol from
nowhere. On a project about Indian payments, with UPI and GST throughout.

Amounts now leave the tools as "INR 1770.20". A unit is part of an amount, not
decoration on it, and the boundary with a language model is exactly where an
implicit convention stops being safe. Internal code keeps the bare string, which
is correct there because every caller already knows the unit.

---

### 2026-09-03 - Optimal assignment never beat greedy on real data.

Implemented the Hungarian algorithm for the case where several bank rows and
several settlements are mutually plausible. Verified against brute force: 300
random matrices, all exact. Built a unit case where greedy strands a row and
optimal does not.

Then measured it on generated data across fifteen contested sets. Optimal was
strictly better zero times. Greedy stranded zero rows.

The reason is structural rather than lucky. My planted ambiguity makes two
settlements share an *exact* amount and the *same* payout window, so every
pairing costs zero and the candidates are genuinely interchangeable. Any
assignment is optimal, and greedy finds one immediately. The cases where optimal
wins need asymmetric costs -- one row strongly preferring a settlement another
row can only just use -- and that shape does not arise here.

So the honest claim is narrower than the one I would have liked to make. The
algorithm is a **guarantee, not an improvement**: it cannot do worse than
greedy, and greedy's failure mode is real and demonstrated in a test, but on
this data it does not occur.

Kept it, for two reasons. The guarantee is worth having in a financial system
where the failure mode is a silently wrong match rather than a visible error.
And measuring that it changes nothing is itself the useful result -- it is the
difference between knowing the ordinary path is safe and assuming it.

Recording this because the tempting version of this entry is "added optimal
assignment for ambiguous matches", which is true, sounds better, and implies an
improvement that fifteen trials say does not exist.

---

### 2026-09-04 - Two tests passed for the wrong reason, and cost money when they failed.

The web layer's degradation tests assert that `/investigate` and `/ask` return
503 with an explanation when no language model is configured. They passed in CI
and in my container, and failed on a machine with `ANTHROPIC_API_KEY` set at
user level -- because there the provider *was* available, the endpoints returned
200, and the agent genuinely investigated three records. The suite took 67
seconds instead of two, and the difference was real model calls.

The test asserted "no key configured" behaviour without ensuring no key was
configured. It was reading the developer's environment as though it were part
of the fixture.

Now cleared explicitly with monkeypatch across all eight provider variables, and
verified to pass both with and without a key present.

Two things worth keeping from this. A test whose result depends on the machine
it runs on is not testing what it claims to test -- it passed for two days in CI
purely because CI has no keys, which is a coincidence rather than a guarantee.
And a suite that spends money when run in the wrong environment is a suite
people quietly stop running, which is a slower and worse failure than a red
build.
