"""
Evaluation harness.

Grades the reconciliation engine against `ground_truth.csv` -- the answer key
written by the generator and never read by the engine.

This is the part of the project that turns a match rate into a measured claim.
Reporting "92% resolved" says nothing about whether the 92% were classified
correctly. Reporting per-class precision and recall against a known answer key
says exactly that, and makes every number falsifiable.

Three levels of reporting:

  1. headline      resolution rate with a Wilson confidence interval
  2. per-class     precision, recall and F1 for each defect class
  3. variance      the same run repeated across independently seeded batches

Run:
    python src/evaluate.py --data data
    python src/evaluate.py --seeds 20          # variance across batches
    python src/evaluate.py --throughput        # scaling behaviour
"""

from __future__ import annotations

import argparse
import csv
import math
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from matcher import Engine, load  # noqa: E402


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

def wilson_interval(successes: int, total: int, z: float = 1.96
                    ) -> tuple[float, float]:
    """
    Wilson score interval for a binomial proportion.

    A match rate measured on 141 records is an estimate, not a constant. The
    normal approximation behaves badly near 0 and 1 and for small samples;
    the Wilson interval does not, which is why it is used here rather than
    the textbook p +/- z*sqrt(p(1-p)/n).
    """
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = (z * math.sqrt(p * (1 - p) / total
                            + z * z / (4 * total * total))) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass
class ClassMetrics:
    """Per-class confusion counts and derived rates."""
    label: str
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def support(self) -> int:
        return self.tp + self.fn


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------

# The engine and the answer key describe the same facts from different angles.
# A few labels are equivalent rather than identical, and mapping them is a
# judgement that belongs here, stated openly, rather than hidden inside the
# matcher where it would inflate the score invisibly.
EQUIVALENT = {
    # the engine reports a missing statement line from the settlement side;
    # the answer key records it against the bank row that was dropped
    ("settlement_not_in_bank", "missing_bank_row"),
    ("missing_bank_row", "settlement_not_in_bank"),
    # a settlement recovered by amount+date when its narration was mangled is
    # a correct resolution; the key records why it was hard, not what it is
    ("clean", "messy_narration"),
}


def load_truth(datadir: Path) -> dict[str, str]:
    with (datadir / "ground_truth.csv").open() as f:
        return {r["entity_id"]: r["expected_classification"]
                for r in csv.DictReader(f)}


def grade(datadir: Path) -> tuple[dict[str, ClassMetrics], dict]:
    orders, txns, settlements, bank = load(datadir)
    engine = Engine(orders, txns, settlements, bank)
    resolutions = engine.run()
    truth = load_truth(datadir)

    metrics: dict[str, ClassMetrics] = defaultdict(
        lambda: ClassMetrics(label=""))
    unmatched_entities: list[tuple[str, str, str]] = []

    graded = 0
    correct = 0

    for r in resolutions:
        expected = truth.get(r.entity_id)
        if expected is None:
            # Not in the answer key. The key records planted defects and clean
            # orders; entities it does not mention are not gradeable and are
            # counted separately rather than silently scored as correct.
            continue

        graded += 1
        got = r.classification
        ok = (got == expected) or ((got, expected) in EQUIVALENT)

        for label in (got, expected):
            if not metrics[label].label:
                metrics[label] = ClassMetrics(label=label)

        if ok:
            correct += 1
            metrics[expected].tp += 1
        else:
            metrics[got].fp += 1
            metrics[expected].fn += 1
            unmatched_entities.append((r.entity_id, expected, got))

    resolved = sum(1 for r in resolutions if r.resolved)
    tiers = Counter(r.tier for r in resolutions if r.resolved)

    lo, hi = wilson_interval(resolved, len(resolutions))
    acc_lo, acc_hi = wilson_interval(correct, graded) if graded else (0, 0)

    summary = {
        "entities": len(resolutions),
        "resolved": resolved,
        "resolution_rate": resolved / len(resolutions) if resolutions else 0,
        "resolution_ci": (lo, hi),
        "graded": graded,
        "correct": correct,
        "accuracy": correct / graded if graded else 0,
        "accuracy_ci": (acc_lo, acc_hi),
        "tiers": dict(tiers),
        "misclassified": unmatched_entities,
    }
    return dict(metrics), summary


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def print_report(metrics: dict[str, ClassMetrics], summary: dict) -> None:
    print("=" * 74)
    print("RECONCILIATION EVALUATION")
    print("=" * 74)
    print()

    lo, hi = summary["resolution_ci"]
    print(f"  entities examined     {summary['entities']}")
    print(f"  resolved              {summary['resolved']} "
          f"({summary['resolution_rate']:.1%})  "
          f"95% CI [{lo:.1%}, {hi:.1%}]")
    print(f"  exceptions            "
          f"{summary['entities'] - summary['resolved']}")
    print()

    alo, ahi = summary["accuracy_ci"]
    print(f"  graded vs answer key  {summary['graded']}")
    print(f"  classified correctly  {summary['correct']} "
          f"({summary['accuracy']:.1%})  "
          f"95% CI [{alo:.1%}, {ahi:.1%}]")
    print()

    print("  resolution by tier")
    tier_names = {0: "self-consistency", 1: "exact key join",
                  2: "deterministic inference", 3: "reference recovery"}
    for t in sorted(summary["tiers"]):
        n = summary["tiers"][t]
        print(f"    tier {t}  {n:>4}  "
              f"({n / summary['resolved']:.1%})  {tier_names.get(t, '')}")
    print()

    print("-" * 74)
    print(f"  {'class':<26}{'prec':>7}{'recall':>8}{'F1':>7}"
          f"{'support':>9}{'FP':>5}{'FN':>5}")
    print("-" * 74)

    for label in sorted(metrics, key=lambda k: -metrics[k].support):
        m = metrics[label]
        if m.support == 0 and m.fp == 0:
            continue
        print(f"  {label:<26}{m.precision:>7.2f}{m.recall:>8.2f}"
              f"{m.f1:>7.2f}{m.support:>9}{m.fp:>5}{m.fn:>5}")
    print("-" * 74)
    print()

    if summary["misclassified"]:
        print("  misclassified entities")
        for eid, expected, got in summary["misclassified"]:
            print(f"    {eid:<14} expected {expected:<24} got {got}")
    else:
        print("  no misclassifications against the answer key")
    print()


# --------------------------------------------------------------------------
# Variance across independently generated batches
# --------------------------------------------------------------------------

def run_variance(n_seeds: int, orders: int, workdir: Path) -> None:
    """
    A single run proves nothing. Regenerate the batch under different seeds
    and report the spread: same defect proportions, different data.
    """
    print("=" * 74)
    print(f"VARIANCE ACROSS {n_seeds} INDEPENDENTLY GENERATED BATCHES")
    print("=" * 74)
    print()

    rates, accs = [], []
    gen = Path(__file__).resolve().parent / "generate_data.py"

    for seed in range(1, n_seeds + 1):
        out = workdir / f"_eval_seed_{seed}"
        subprocess.run(
            [sys.executable, str(gen), "--seed", str(seed),
             "--orders", str(orders), "--out", str(out)],
            check=True, capture_output=True)
        _, summary = grade(out)
        rates.append(summary["resolution_rate"])
        accs.append(summary["accuracy"])
        print(f"  seed {seed:>3}   resolved {summary['resolution_rate']:>6.1%}"
              f"   accuracy {summary['accuracy']:>6.1%}"
              f"   entities {summary['entities']:>4}")

    def stats(xs):
        mean = sum(xs) / len(xs)
        var = sum((x - mean) ** 2 for x in xs) / len(xs)
        return mean, math.sqrt(var), min(xs), max(xs)

    rm, rs, rmin, rmax = stats(rates)
    am, asd, amin, amax = stats(accs)

    print()
    print("-" * 74)
    print(f"  resolution rate   {rm:.1%} +/- {rs:.1%}   "
          f"range [{rmin:.1%}, {rmax:.1%}]")
    print(f"  accuracy          {am:.1%} +/- {asd:.1%}   "
          f"range [{amin:.1%}, {amax:.1%}]")
    print("-" * 74)
    print()


# --------------------------------------------------------------------------
# Throughput
# --------------------------------------------------------------------------

def run_throughput(sizes: list[int], workdir: Path) -> None:
    """
    The track bar names throughput explicitly. Measured, not asserted.
    """
    print("=" * 74)
    print("THROUGHPUT")
    print("=" * 74)
    print()
    print(f"  {'orders':>8}{'entities':>10}{'generate':>12}"
          f"{'reconcile':>12}{'rows/sec':>12}")
    print("-" * 74)

    gen = Path(__file__).resolve().parent / "generate_data.py"

    for n in sizes:
        out = workdir / f"_eval_scale_{n}"
        t0 = time.perf_counter()
        subprocess.run(
            [sys.executable, str(gen), "--seed", "42",
             "--orders", str(n), "--out", str(out)],
            check=True, capture_output=True)
        t_gen = time.perf_counter() - t0

        orders, txns, settlements, bank = load(out)
        t1 = time.perf_counter()
        resolutions = Engine(orders, txns, settlements, bank).run()
        t_rec = time.perf_counter() - t1

        print(f"  {n:>8}{len(resolutions):>10}{t_gen:>11.2f}s"
              f"{t_rec:>11.3f}s{len(resolutions) / t_rec:>12,.0f}")
    print("-" * 74)
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--seeds", type=int, default=0,
                    help="run variance analysis across N seeds")
    ap.add_argument("--orders", type=int, default=120)
    ap.add_argument("--throughput", action="store_true")
    ap.add_argument("--workdir", default=".eval")
    args = ap.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(exist_ok=True)

    if args.seeds:
        run_variance(args.seeds, args.orders, workdir)
    elif args.throughput:
        run_throughput([120, 500, 1000, 5000], workdir)
    else:
        metrics, summary = grade(Path(args.data))
        print_report(metrics, summary)


if __name__ == "__main__":
    main()
