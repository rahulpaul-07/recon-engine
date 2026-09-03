# Architecture

A reconciliation engine for Indian payments. It reads a merchant's order
ledger, a payment gateway's transaction report, the gateway's settlement
report and the corresponding bank statement, determines which records
correspond to which, explains every discrepancy it can, and reports the ones it
cannot with a stated reason.

This document describes how it works and why it is built this way. Design
decisions with their rejected alternatives are in `DECISIONS.md`; the record of
what broke during construction is in `NOTES.md`.

---

## 1. The problem

Three organisations record the same sale, and none of their records agree.

A customer pays ₹450 for coffee. The merchant's database records order 4471,
₹450, paid, 14 August. The gateway records payment `pay_XYZ`: ₹450 captured,
₹10.62 retained as MDR plus GST, ₹439.38 payable. The bank records a single
credit of ₹38,204.55 on 16 August.

None of them is wrong. The merchant records gross because that is what the
customer paid, and its checkout has no visibility of commercial fee terms. The
gateway records net because that is what it owes. The bank records a lump
because gateways batch a day's payments, deduct fees and refunds, and send one
transfer — and because a payment aggregator may not hold funds, the RBI
dictates when that transfer leaves escrow, typically T+1 working day.

Two structural consequences follow, and the whole design exists to handle them.

**Granularity mismatch.** The ledger has 87 rows, the gateway has 87 rows, the
bank has one. There is no row-to-row join across all three; a *group* must be
matched to a row and its total proven.

**Timing mismatch.** The sale is 14 August and the money is 16 August. Matching
on date equality fails everywhere; matching on any date pairs orders with the
wrong day's settlement.

On top of that sit the cases that make the problem genuinely hard rather than
merely tedious: refunds flowing backwards into later settlements, chargebacks
reversing a sale weeks out of period with a penalty attached, duplicate
captures that exist temporarily before one is voided, captures near the end of
a window that are *correctly* unmatched because they have not been paid out
yet, and sub-paise rounding drift that must be tolerated rather than reported.

---

## 2. Data model

Four input files and an answer key. Every field exists for a reason; the ones
that matter are called out.

### `ledger.csv` — the merchant's own book

| Field | Note |
|---|---|
| `order_id` | primary key |
| `order_amount_paise` | **gross** — the merchant does not know about fees |
| `currency` | INR |
| `order_datetime` | anchors the settlement window |
| `customer_id` | aids duplicate detection |
| `order_status` | paid / failed / refunded / cancelled |
| `payment_method` | card / upi / netbanking / wallet |

`payment_method` is load-bearing. MDR differs by method, so the expected fee
cannot be derived from the amount alone. A system hardcoding a single
percentage is wrong on three of the four methods.

### `gateway.csv` — the aggregator's report

This file has more row types than the ledger, and that asymmetry is the problem
in one line.

| Field | Note |
|---|---|
| `txn_id` | `pay_…`, `rfnd_…`, `cb_…` |
| `txn_type` | payment / refund / chargeback / chargeback_reversal / adjustment |
| `order_ref` | **nullable** — real reports lose it |
| `gross_amount_paise` | **signed** — negative for refunds and chargebacks |
| `fee_paise`, `gst_on_fee_paise` | GST stored separately; it is separately reported for input credit |
| `net_amount_paise` | `gross − fee − gst`. **Redundant deliberately** |
| `settlement_id` | **nullable** — null means not yet paid out |
| `status` | captured / failed / voided / refunded / processed / lost |

Three choices worth defending:

**Signed amounts.** A refund is `−45000`, not a positive value with a type
flag. A settlement total is then a plain sum with no branching on transaction
type. Conditional sign handling is the easiest place to introduce a bug that
produces a plausible-looking wrong total.

**`net` is redundant on purpose.** It is derivable, and it is stored anyway so
the engine can verify the *gateway's own* arithmetic and catch internally
inconsistent rows.

**`settlement_id` nullable, and null is not an error.** A capture in the last
day or two of the window has not been paid out yet. Those rows are *correctly*
unmatched. Conflating "not yet settled" with "should have settled and did not"
would fill the exception list with records behaving perfectly.

### `settlements.csv` — the payout report

`settlement_id`, `capture_date`, `payout_date`, `total_paise`, `utr`.

This file did not exist in the first design. Gateway rows carry a
`settlement_id` and bank rows carry a UTR, and nothing connected them. Real
gateways publish exactly this report. The gap was found by writing the
matcher's interface before its implementation.

### `bank.csv` — deliberately the poorest source

`value_date` is a date with **no time**, because banks do not give timestamps.
`description` is unstructured free text, sometimes truncated. `utr` is
**nullable**. `balance_paise` is a running balance, which is what makes a
*missing* row detectable at all.

`movement_paise` is computed as `credit − debit`, because a settlement can be
**net negative** on a refund-heavy day and appear as a debit. Code that reads
only credits reports every such day as a missing settlement.

### `ground_truth.csv` — the answer key

Every planted defect, recorded with its expected classification. **Never read
by the reconciliation engine.** This file is why accuracy here is measured
rather than asserted, and it is the single largest methodological difference
between this project and a reconciliation tool evaluated by eyeballing its
output.

---

## 3. Core invariants

Three rules in `src/core.py`, shared by the generator and the engine so the two
cannot drift apart.

**Money is integer paise.** `0.1 + 0.2 != 0.3` under IEEE-754. Summing 87
payments and comparing against a bank credit in floating point produces
sub-paise drift on every settlement. Fees are computed in `Decimal` with an
explicit rounding rule, then converted to integer paise for storage and
comparison. This is also how Razorpay's own API represents amounts.

**Fees are a rule table, not a constant.** Percentage for cards and wallets,
flat ₹12 per transaction for netbanking, zero for UPI. A table rather than a
constant because zero-MDR on UPI is no longer legally fixed: the Taxation and
Other Laws (Amendment) Bill, 2026 amended section 10A of the Payment and
Settlement Systems Act, replacing the blanket prohibition with
government-notified exemptions. A rule table absorbs that change; a hardcoded
rate does not.

Fee and GST are rounded independently, `ROUND_HALF_UP`. That is a stated
choice, which is why a two-paise tolerance exists — independent rounding on
either side can legitimately differ, and reporting a hundred one-paise
exceptions would be useless.

**Settlement is T+1 *working* days.** Weekends and holidays push it out. A
Friday capture settles Monday. This is what forces the matcher to reason about
a date *window* rather than date equality.

One scope rule that caused a real bug: the identity `gross − fee − gst == net`
holds **only** for rows that moved money. A failed attempt reports the
attempted gross with `net = 0`. Checking it unfiltered raised a false exception
on every failed payment.

---

## 4. The pipeline

```
ledger   gateway   settlements   bank
   └────────┴─────┬────┴──────────┘
                  ▼
           tiered matcher
   tier 0  single-source consistency
   tier 1  exact key joins              124
   tier 2  deterministic inference        4
   tier 2b optimal assignment (contested)
                  ▼
         unresolved records (13)
                  ▼
     resolution agent — 9 tools, ≤5 rounds
                  ▼
           verification gate
    exists? · amount ties? · date in window?
                  ▼
  resolved (evidence)  |  escalated (analyst note)
```

**Every resolution records the tier that produced it.** That is what makes the
output confidence-stratified rather than one opaque percentage: 124 by exact
key, 4 by inference.

### Tier 0 — single-source consistency

Checks that need only one file. Gateway arithmetic, filtered to money-moving
statuses. Bank statement continuity: if
`balance[n] − balance[n−1] != movement[n]`, a line is missing from the
statement entirely.

A missing line is reported against the **gap** (`GAP_BEFORE_<row>`) rather than
against the row that reveals it. Reporting it against the row overwrites that
row's own classification, which silently corrupted messy-narration and orphan
rows that happened to follow a dropped line.

### Tier 1 — exact key joins

Per order, in order: no payment at all → `missing_payment`; multiple payments →
`duplicate`; failed; method disagreement; amount disagreement; fee beyond
tolerance → `fee_mismatch`; fee within tolerance but nonzero →
`rounding_noise`; chargeback; refund or partial refund; null settlement →
`unsettled`; otherwise `clean`.

Then settlement totals against member sums, then bank rows joined to
settlements on UTR.

### Tier 2 — deterministic inference

For bank rows with no usable UTR: find settlements whose total equals the
movement **exactly** and whose payout window contains the value date. Exactly
one candidate is accepted and tagged tier 2. Zero candidates →
`orphan_bank_credit`.

### Tier 2b — optimal assignment

Where several rows and several settlements are mutually plausible, matching one
at a time can commit to a pair a later row needed more. That is the assignment
problem; the Hungarian algorithm solves it in O(n³) and returns a provably
optimal pairing. Implemented directly rather than pulled from a library, and
verified against brute force on 300 random matrices.

The cost function weights amount difference 1000× against date drift 1×, and
treats a non-tying amount as **inadmissible** rather than expensive.
Reconciliation has no notion of a nearly correct amount, and settlement dates
move for mundane reasons while amounts do not.

**The honest claim is narrower than it sounds.** Measured across fifteen
contested sets, optimal was never better than greedy, because the planted
ambiguity makes settlements share an exact amount and window — so every pairing
costs the same and any assignment is optimal. It is a guarantee that greedy
cannot do worse, not a measured improvement.

---

## 5. Where the language model sits

Two places, with **different strengths of guarantee**. The difference is
deliberate and is the most important thing in this document.

### Reference recovery (`src/narration.py`)

Bank narration is unstructured. Four named regex patterns handle the clean
majority; a truncated hit returns a **prefix**, not an identifier, so the
verifier attempts unique completion rather than treating a partial string as a
match. Only what regex cannot parse reaches a model, under a narrow prompt:
extract a token, do not reason about matching, do not invent.

**Then the verification gate.** Three checks, all arithmetic the proposer
cannot influence: the reference must resolve to exactly one known settlement,
the amount must tie exactly, the date must fall inside the plausible payout
window.

Running with `--no-verify` shows what the gate prevents: two well-formed
references corresponding to nothing are accepted on trust, fabricating ₹6,950
of matches and making the books appear to balance while two genuine breaks
disappear.

The gate was built to guard the language model. It first caught a **regex**
result. Extraction succeeding is not the same as the extracted value being
correct, and confidence in the extraction method is irrelevant to that.

### Exception resolution (`src/agent.py`)

Records the deterministic tiers cannot resolve go to a bounded agent that
selects its own investigation tools from a registry of nine. It answered all 13
exceptions on the reference batch and reached the same classification as the
deterministic engine on 12 (Cohen's κ 0.90, an unstable estimate at this sample
size).

Four constraints, **enforced in code rather than requested in the prompt**:

| Constraint | Enforcement |
|---|---|
| Step limit | 5 model rounds, then escalate — never guess |
| Closed tool registry | anything outside it returns "not available" |
| Fixed taxonomy | 18 values; an invented classification is downgraded |
| Evidence required | `resolved: true` with zero tool calls is forced to `false` |

Because they are code, all four are tested by calling the verdict parser
directly with the output a misbehaving model would produce — no network, no API
key. A constraint that can only be verified by running a model and hoping is
not a constraint.

**The division: the agent contributes investigative strategy; the tools
contribute truth.** The model performs no arithmetic and asserts no
relationship a tool has not confirmed. A model failure degrades an exception
into an escalation, never into a wrong number.

### Settlement Q&A (`src/ask.py`) — a weaker guarantee, stated

Plain-English questions over aggregate queries. Same shape: the model
translates and explains, the code computes.

But its rule against combining figures lives in the **prompt**, not in code.
Across three runs of the same five questions it held completely, partially, and
not at all. On one run it added two tool results and reported the sum —
correctly, which is worse than incorrectly, because a right answer produced by
a forbidden route is indistinguishable from a grounded one.

This is documented rather than hidden. The fix is a tool returning combined
totals so no addition is ever needed, or a post-check that every number in the
answer appears in a tool result. Both are code-level and testable; neither is
built.

---

## 6. Provider independence

Seven providers across twenty provider/model candidates. Anthropic and Gemini
use native SDKs; Groq, Cerebras, OpenRouter, Together and Mistral speak the
OpenAI dialect and share one adapter — five configurations of one class, so the
awkward part (translating Anthropic's tool-result blocks into OpenAI's separate
`tool` messages) is written and debugged once.

Failover is **per call and scoped by failure kind**:

- a retired or unknown model demotes **that model**; the next model on the same
  provider is tried
- an unfunded account, revoked key or exhausted quota demotes the **whole
  provider**, and its remaining models are skipped
- an unrecognised error is treated as model-level, the conservative choice

Each candidate is attempted once per session. Retrying a dead candidate on
every record wastes the run and buries the real error.

This was rebuilt after a live failure: a valid Groq key with a retired model
name caused the original chain to discard an otherwise working provider. Model
identifiers are a dependency on a vendor's catalogue at a point in time, and
vendors retire models.

**`none` is a first-class provider.** With no key configured the system runs
its deterministic paths and states plainly that model-dependent steps were not
attempted. CI has a job that sets no API keys and asserts the engine still
reconciles correctly — graceful degradation is a designed path, so it is tested
like one.

---

## 7. Evaluation

The generator plants each defect deliberately and records it in
`ground_truth.csv`, which the engine never reads. Every figure below is
measured against that key rather than asserted.

| | |
|---|---|
| Batch | 120 orders, 141 reconcilable entities |
| Resolved | 90.8% (95% CI 84.9–94.5%) |
| Classification accuracy | 100.0% across 14 classes |
| Across 12 independent batches | 92.7% ± 0.4% resolved, 100.0% ± 0.0% accuracy |
| Throughput | ~233,000 entities/sec, flat from 141 to 5,022 |
| Tests | 87, verified by mutation |

Confidence intervals are Wilson score rather than the normal approximation,
which behaves badly near 1 and on small samples.

### What the accuracy figure does not show

It measures whether the engine identifies the defect classes it was **designed
around**, on data generated by the same author. It is evidence of no false
positives and no silently discarded records. It is not evidence of performance
on a real merchant's books, where defect types the generator does not model
would appear.

### Where it breaks

Raising defect density alone does not degrade accuracy — each record is
classified independently, so more defects of the same kinds change nothing. The
first stress test returned 100% at every density, which was a sign the
experiment was wrong rather than a good result.

Defects that *interact* do degrade it:

| Compound defect density | Accuracy |
|---|---|
| 22% | 100.0% |
| 37% | 98.6% |
| 52% | 97.6% |
| 62% | 95.7% |
| 82% | 92.3% |

Every failure has the same shape: an order carrying both a fee mismatch and a
refund is reported as one or the other, because the taxonomy allows a single
label per record. Neither answer is wrong. The limitation is in the
classification scheme, not the matcher, and the fix is to return a set of
labels and grade against set overlap.

### Mutation verification

Passing tests prove nothing unless they fail when the code is wrong. Three
deliberate bugs were introduced and each was caught: widening the fee tolerance
until it absorbed a genuine overcharge (1 test), removing the amount check from
the verification gate (1 test), and reverting failover to demote a whole
provider on any failure (3 tests, including one written for that specific
regression).

---

## 8. Known limitations

Stated rather than discovered by a reader.

1. **Single-label taxonomy.** A record with two simultaneous conditions can
   only be reported as one. Dominant error mode at high compound defect density.
2. **The Q&A layer's arithmetic rule is prompt-level**, therefore probabilistic.
   Demonstrated failing.
3. **The answer key encodes assumptions about the engine.** Adding optimal
   assignment invalidated two ground-truth entries without touching the data,
   because the key recorded a planted *condition* rather than an expected
   *outcome*.
4. **Aggregates have no falsifier.** Record-level checks are graded against the
   answer key; aggregate queries are not. A fee-summary bug reporting 1.85% MDR
   on zero-MDR UPI was caught only because that rate was known to be zero.
5. **The agent is nondeterministic.** Identical input has produced different
   investigation paths and, once, a different label. Both were defensible; the
   behaviour is not reproducible.
6. **Synthetic data throughout.** No real merchant's books have been reconciled.

---

## 9. Module map

```
src/core.py           money, fee rules, working-day calendar
src/generate_data.py  synthetic batch + ground-truth answer key
src/matcher.py        tiered reconciliation engine
src/assignment.py     Hungarian solver for contested matches
src/narration.py      reference recovery with verification gate
src/llm.py            provider-agnostic interface with scoped failover
src/tools.py          9 deterministic investigation tools
src/agent.py          bounded exception resolution agent
src/investigate.py    runs the agent over unresolved records
src/ask.py            settlement Q&A over aggregate queries
src/evaluate.py       grading, Wilson intervals, variance, stress, kappa
src/report.py         self-contained HTML report
```

Only `investigate.py` and `ask.py` require a language model. Everything else is
deterministic and runs with no API key and no network access.

---

## 10. Extending it

**A new defect class** needs a planting rule in the generator, a detection rule
in the matcher tier where it belongs, and an entry in the agent's taxonomy.
The answer key updates itself from the generator.

**A new provider** is a subclass of `OpenAICompatibleProvider` with a base URL,
environment variable and model list — four lines if it speaks the OpenAI
dialect.

**A new investigation tool** needs a method on `InvestigationTools`, an entry in
`TOOL_SCHEMA` describing what it *proves* rather than what it returns, and a
line in the dispatch table. The closed-registry test will fail if the three
fall out of step.

**Real merchant data** would need a loader per gateway format, and the fee rule
table replaced with the merchant's contracted rates. The matcher itself is
format-agnostic once the data is in the internal shape.
