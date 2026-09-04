# Tests

```bash
pip install pytest httpx2==2.12.0
python3 -m pytest tests/ -q
```

httpx2 is required by Starlette's TestClient, which the web-interface
tests use. The engine itself needs neither it nor any other dependency.

104 tests, no network access and no API key required. The agent's constraints are
enforced in code rather than requested in the prompt, so all of them are tested
by calling that code directly with the output a misbehaving model would produce.

## What is tested, and why

**Money and fee rules** are tested against invariants rather than examples: that
summation is exact where floating point is not, that a flat netbanking fee does
not scale with amount, that GST is computed on the fee rather than the gross,
and that the fee tolerance is smaller than the smallest genuine discrepancy the
generator plants.

**The matcher** is tested for the properties that matter more than any single
classification: that every entity is reported exactly once, that no unresolved
record lacks a stated reason, that correctly-unmatched rows are not treated as
errors, and that a genuine break is never marked resolved.

**The verification gate** is tested against each of its three checks
independently, plus the case that motivated it -- a well-formed reference,
extracted cleanly, corresponding to no settlement at all.

**Failover** is tested for the distinction the design turns on: a retired model
identifier must cost that model only, while a dead account costs the provider.
The regression that prompted the redesign has its own test.

## Mutation checks

Tests that pass tell you nothing unless they fail when the code is wrong. Three
deliberate bugs were introduced and each was caught:

| Mutation | Caught by |
|---|---|
| Fee tolerance widened to absorb a real overcharge | 1 test |
| Amount check removed from the verification gate | 1 test |
| Failover demotes the whole provider on any failure | 3 tests |
