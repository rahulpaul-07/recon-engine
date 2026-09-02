# Design decisions

Each entry records what was chosen, why, and what was rejected. Rejected
alternatives are recorded deliberately — a decision without an alternative
isn't a decision.

---

## D1 — Python, not C++

**Chosen:** Python 3.12, `decimal` for fee computation, integer paise for storage.

**Why:**
- Exact decimal arithmetic is in the standard library. C++ has no standard
  decimal type; representing money correctly would mean hand-rolling fixed-point
  arithmetic before writing any reconciliation logic.
- The LLM layer (Tier 3) needs a mature SDK. These are Python-first.
- CSV parsing, timezone-aware dates, working-day calendars and grouped
  aggregation are all one line each.

**Rejected:** a C++ matching core with Python bindings. The bottleneck in this
system is correctness and I/O, not CPU — a 50–500 row batch is not a performance
problem. Adding an FFI boundary would have introduced a reliability risk for no
measurable gain.

**Where the algorithmic work actually lives:** subset-sum for composing a bank
credit from its member payments, bipartite matching for ledger↔gateway pairing
under constraints, interval reasoning for settlement date windows. The language
doesn't change any of that.

---

## D2 — Money is integer paise, never float

**Chosen:** store and compare all amounts as `int` paise. Compute fees in
`Decimal`, round explicitly, then convert to `int`.

**Why:** IEEE-754 binary floating point cannot represent most decimal fractions
exactly (`0.1 + 0.2 == 0.30000000000000004`). Summing 87 payments and comparing
to a bank credit would produce spurious sub-paise mismatches and turn every
settlement into a false exception.

This is also how Razorpay's own API represents amounts — `amount: 45000` for
₹450.00.

**Rounding rule:** `ROUND_HALF_UP` at the paise level, applied independently to
the fee and to GST on that fee. This is a *choice*; real systems differ, which is
a genuine source of one-paise drift. The generator deliberately plants ±1 paise
noise on some rows so the matcher has to tolerate it rather than report it.

---

## D3 — Fees are method-dependent, not a flat percentage

**Chosen:** a fee rule table keyed by payment method — percentage for cards and
wallets, flat per-transaction for netbanking, zero for UPI.

**Why:** a hardcoded 2% is the clearest signal of a synthetic toy dataset. Real
MDR varies by method, and UPI has been zero-MDR since January 2020.

This also matters commercially right now: the Taxation and Other Laws
(Amendment) Bill, 2026 amended Section 10A of the Payment and Settlement Systems
Act, 2007, removing the blanket prohibition on MDR for UPI and RuPay and
replacing it with government-notified exemptions. The zero-rate is no longer
legally fixed. A rule table absorbs that change; a hardcoded constant doesn't.

**Consequence for the matcher:** expected fee cannot be derived from amount
alone. `payment_method` is load-bearing, and a method disagreement between
ledger and gateway is itself an exception class.

---

## D4 — Refunds and chargebacks are negative signed amounts

**Chosen:** a refund is `gross_amount_paise = -45000`, not a positive value with
a type flag.

**Why:** a settlement total is then the plain sum of its member rows, with no
branching on transaction type. Sign errors in conditional arithmetic are the
single easiest bug to introduce and the hardest to spot in a total that looks
plausible.

**Consequence:** a settlement can be **net negative** on a refund-heavy day, and
appears on the bank statement as a debit. Any code assuming money only flows
toward the merchant is wrong. (This already caused a real bug — see NOTES.md.)

---

## D5 — `net_amount_paise` is redundant on purpose

`net` is derivable from `gross - fee - gst`. It is stored anyway so the engine
can verify the gateway's own arithmetic and catch internally inconsistent rows.

**Scope rule:** the identity holds only for rows that moved money
(`captured` / `processed` / `lost`). A failed attempt reports the attempted gross
with `net = 0`. The check must filter on status first.

---

## D6 — `settlement_id` is nullable, and null is not an error

A captured payment with `settlement_id = NULL` has not been paid out yet — the
normal state for anything in the last day or two of the window.

These rows are **correctly unmatched**. Distinguishing "not yet settled" from
"should have settled and didn't" is a real requirement, and conflating them would
inflate the exception list with rows that are behaving perfectly.

---

## D7 — Bank narration is deliberately messy

Bank statement descriptions are generated from a mix of clean and degraded
templates: truncated UTRs, stray spacing, and some with no UTR at all.

**Why:** this is the residual where a language model legitimately earns its place
— recovering a reference from unstructured free text. It is a far more defensible
use of an LLM than asking one to perform the matching itself.

---

## D8 — The running balance column is a detector, not decoration

`balance_paise` lets the engine detect rows *missing from the statement
entirely*: if `balance[n] - balance[n-1] != credit[n] - debit[n]`, a line is
absent. The generator drops one row deliberately to exercise this.

---

## D9 - The language model provider is swappable, and "none" is a valid choice

**Chosen:** one interface (`src/llm.py`) over Anthropic, Groq and Gemini, plus
an explicit null provider. Selection is by environment variable, with an
override for forcing a specific vendor.

**Why:** a reconciliation run must not fail because one vendor is unreachable.
For a payments company a provider outage is an ordinary Tuesday, not an
exceptional condition, so degradation has to be a designed path rather than an
error branch.

Tool schemas are written once in Anthropic's format and translated at the
provider boundary. Keeping one source of truth matters more than avoiding the
translation: duplicating the schema per vendor would let the definitions drift
apart silently, which is precisely the class of bug this project exists to
detect elsewhere.

**`none` is a first-class provider.** With no key configured the system runs
its deterministic paths and states plainly that model-dependent steps were not
attempted. A demo that silently changes behaviour when a key is absent is worse
than one that says so.

**Rejected:** calling a single vendor's SDK directly. Simpler, and it makes the
whole project hostage to one signup, one rate limit and one outage.

**Known limitation:** Gemini's multi-turn tool protocol differs enough that the
adapter flattens the exchange into a single prompt rather than translating it
fully. Adequate for the short bounded loops here, and stated in the module
rather than hidden.

---

## D10 - Provider failover is per call, and a failed provider is demoted

**Chosen:** a `FallbackChain` over every configured provider. On error the next
one is tried within the same call, and the failing provider is demoted for the
rest of the session.

**Why the original design was not enough:** selecting a provider once at startup
survives a missing key and nothing else. The first live run of this project
failed on a key that was valid but attached to an account with no credit -- a
condition indistinguishable from a working setup until the call is made. A rate
limit hit on the fourth step of a five-step investigation would fail the same
way.

**Why demote rather than retry:** thirteen exceptions retrying two dead
providers is twenty-six doomed calls before any work happens, and the genuine
error ends up buried under repeats. One attempt per provider per session is
enough to establish it is unusable.

**Why failovers are reported:** silently answering from a different vendor than
the operator expected is its own failure. Every demotion is recorded and printed
with the run.

Seven providers are supported. Five of them speak the OpenAI chat dialect and
are expressed as configuration of one adapter rather than five classes, so the
awkward part -- translating Anthropic's tool-result blocks into OpenAI's
separate `tool` messages -- is written and debugged once.

---

## D11 - Failover walks provider AND model, and demotion is scoped by failure kind

**Chosen:** each provider declares a list of models. The chain walks
provider/model pairs -- twenty by default across seven vendors -- and classifies
each failure before deciding what to demote.

- A retired or unknown model identifier demotes **that model only**. The
  provider stays live and the next model on it is tried.
- An unfunded account, revoked key or exhausted quota demotes **the whole
  provider**, and its remaining models are skipped rather than each failing
  identically.
- An unrecognised error is treated as model-level, which is the conservative
  choice: it costs one candidate instead of discarding a provider that may work
  with a different model.

**Why:** the first version demoted whole providers on any error. The first live
run hit a valid Groq key with a retired model name and threw the provider away
over a 404. The key was fine; only the identifier was stale.

Model identifiers are a dependency on a vendor's catalogue at a point in time,
and vendors retire models on their own schedule -- Groq deprecated
`llama-3.3-70b-versatile` in June 2026. Treating that as equivalent to a dead
account conflates two failures with completely different remedies.

Every candidate is overridable per provider by environment variable, so a future
retirement is configuration rather than a code change.

---

## D12 - Agent constraints are enforced in code so that they are testable

**Chosen:** every constraint on the agent -- step limit, closed tool registry,
fixed classification taxonomy, evidence required for a claimed resolution -- is
enforced in the calling code rather than requested in the prompt.

**Why:** a constraint expressed in a prompt can only be verified by running a
model and hoping. A constraint expressed in code can be tested directly, by
passing it the output a misbehaving model would produce. The test suite calls
the verdict parser with an invented classification, with a resolution claimed
against zero tool calls, with unparseable text, and with malformed JSON. None of
those tests needs a network connection or an API key.

This is also why the suite runs in under a second and can be part of an ordinary
development loop.

**Verified by mutation.** Three deliberate bugs were introduced and each was
caught: widening the fee tolerance until it absorbed a genuine overcharge,
removing the amount check from the verification gate, and reverting failover to
demote a whole provider on any failure. The last was caught by three tests
independently, including one written specifically for the regression that
prompted the redesign.
