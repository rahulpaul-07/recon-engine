# Three-way payment reconciliation engine

Reconciles a merchant's order ledger against a payment gateway's transaction
report and the corresponding bank statement, resolves what it can, and reports
an honest, categorised list of what it could not.

## Status

Work in progress.

- [x] Synthetic data generator with planted defects and ground-truth answer key
- [ ] Tier 1 matcher: exact key joins
- [ ] Tier 2 matcher: deterministic inference (amount, date window, subset-sum)
- [ ] Tier 3: reference recovery from unstructured bank narration
- [ ] Evaluation harness: match rate, per-class precision and recall
- [ ] Exception report

## Why this problem

Three systems record the same sale and none of them agree. The merchant's
ledger records the gross amount. The gateway records the same payment less its
fee. The bank records a single netted credit covering an entire day of
payments, one or more working days later. Refunds, chargebacks, duplicates and
unsettled transactions all break the correspondence in different ways.

Reconciling these is still largely manual work below a certain company size.

## Quick start

    python3 src/generate_data.py --seed 42 --orders 120

Writes `ledger.csv`, `gateway.csv`, `bank.csv` and `ground_truth.csv` to `data/`.
The ground-truth file is the answer key for evaluation and is never read by the
reconciliation engine.

## Documents

- `DECISIONS.md` - design decisions, with rejected alternatives
- `NOTES.md` - running log of what broke and how it was resolved
