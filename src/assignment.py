"""
Optimal assignment for ambiguous matches.

Tier 2 matches a bank row to a settlement by amount and payout window. Usually
exactly one candidate fits and the match is unambiguous. Occasionally several
bank rows and several settlements are mutually plausible -- same amount, same
window -- and the question becomes which pairing to choose.

Greedy matching answers this one row at a time, committing to the best pair it
sees first. That can be wrong: an early choice may consume a settlement that a
later row needed more, forcing the later row into a worse pairing or none at
all. The error is not in any individual decision; it is in deciding
sequentially at all.

The assignment problem asks for the pairing that minimises total cost across
every row simultaneously. The Hungarian algorithm solves it in O(n^3) and
returns a provably optimal assignment.

What "optimal" means here
-------------------------
Optimal with respect to the cost matrix, and no further. The algorithm answers
the question it is given exactly; whether that is the right question depends on
the cost function, which is a modelling judgement rather than a mathematical
one. The cost used here is stated explicitly below and is deliberately simple.

Because of that, this module does not decide anything on its own. It proposes a
pairing, and every pair still passes the same verification the deterministic
tiers apply: the amount must tie exactly and the date must fall inside the
plausible window. An assignment that is optimal but does not verify is
discarded. The algorithm chooses among admissible pairings; it cannot create
one.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import paise_to_rupees_str, working_day_window  # noqa: E402

# A cost this large means "not admissible". Any pairing containing one is
# rejected after solving rather than being allowed to influence the solution.
IMPOSSIBLE = 10 ** 9


@dataclass
class Pairing:
    row_index: int
    settlement_index: int
    cost: int
    admissible: bool
    reason: str = ""


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------

def pair_cost(movement_paise: int, value_date: date,
              settlement_total_paise: int, capture_date: date) -> tuple[int, str]:
    """
    Cost of pairing one bank row with one settlement.

    Two components, and the weighting between them is the modelling decision
    worth defending:

      * amount difference, in paise, weighted 1000x
      * days between the observed date and the nearest end of the plausible
        payout window, weighted 1x

    Amount dominates deliberately. A one-paise difference is a stronger signal
    of a wrong pairing than a one-day difference, because settlement dates move
    for mundane reasons -- a bank holiday, a late file -- while amounts do not
    move at all. Weighting them equally would let a large amount error be
    excused by a good date, which is exactly the wrong trade in reconciliation.

    An amount that does not tie exactly is inadmissible rather than expensive.
    Reconciliation has no notion of a nearly correct amount.
    """
    if movement_paise != settlement_total_paise:
        return IMPOSSIBLE, (
            f"amount {paise_to_rupees_str(movement_paise)} does not tie to "
            f"{paise_to_rupees_str(settlement_total_paise)}")

    lo, hi = working_day_window(capture_date)
    if value_date < lo:
        drift = (lo - value_date).days
    elif value_date > hi:
        drift = (value_date - hi).days
    else:
        drift = 0

    if drift > 7:
        return IMPOSSIBLE, (f"{value_date} is {drift} days outside the "
                            f"payout window {lo}..{hi}")

    return drift, (f"amount ties exactly, {drift} day(s) from the window"
                   if drift else "amount ties exactly, inside the window")


# --------------------------------------------------------------------------
# Hungarian algorithm
# --------------------------------------------------------------------------

def hungarian(cost: list[list[int]]) -> list[int]:
    """
    Solve the rectangular assignment problem, minimising total cost.

    Implemented directly rather than pulled from scipy: the whole project has
    no third-party runtime dependency for the deterministic path, and adding
    one for a function used on a handful of rows is a poor trade.

    This is the O(n^3) shortest-augmenting-path formulation (Jonker-Volgenant
    style potentials). `u` and `v` are dual potentials, one per row and column;
    the invariant they maintain is that `cost[i][j] - u[i] - v[j] >= 0` for all
    pairs, with equality on the pairs currently selected. Each iteration adds
    one row to the assignment by finding a shortest augmenting path in the
    reduced-cost graph, then shifts the potentials so the invariant holds again.

    Returns a list where entry i is the column assigned to row i, or -1 if the
    row is unassigned (possible only when there are fewer columns than rows).
    """
    if not cost or not cost[0]:
        return []

    n, m = len(cost), len(cost[0])
    if n > m:                       # solve the transpose, then invert
        t = [[cost[i][j] for i in range(n)] for j in range(m)]
        back = hungarian(t)
        out = [-1] * n
        for j, i in enumerate(back):
            if i != -1:
                out[i] = j
        return out

    INF = float("inf")
    u = [0.0] * (n + 1)             # row potentials
    v = [0.0] * (m + 1)             # column potentials
    p = [0] * (m + 1)               # p[j] = row currently assigned to column j
    way = [0] * (m + 1)             # predecessor column, for path recovery

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)

        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, -1

            for j in range(1, m + 1):
                if used[j]:
                    continue
                # Reduced cost of pairing row i0 with column j.
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j], way[j] = cur, j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j

            # Shift potentials by delta so the invariant is restored.
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta

            j0 = j1
            if p[j0] == 0:          # reached a free column: path complete
                break

        while j0:                   # walk the augmenting path back
            j1 = way[j0]
            p[j0], j0 = p[j1], j1

    assignment = [-1] * n
    for j in range(1, m + 1):
        if p[j] > 0:
            assignment[p[j] - 1] = j - 1
    return assignment


# --------------------------------------------------------------------------
# Greedy, for comparison
# --------------------------------------------------------------------------

def greedy(cost: list[list[int]]) -> list[int]:
    """
    Match one row at a time, taking the cheapest available column.

    Present so the difference can be measured rather than asserted. This is
    what the engine did before, and on most batches it produces the same answer
    as the optimal solver -- which is itself worth knowing.
    """
    n, m = len(cost), len(cost[0]) if cost else 0
    taken: set[int] = set()
    out = [-1] * n
    for i in range(n):
        best, best_j = IMPOSSIBLE, -1
        for j in range(m):
            if j not in taken and cost[i][j] < best:
                best, best_j = cost[i][j], j
        if best_j != -1 and best < IMPOSSIBLE:
            out[i] = best_j
            taken.add(best_j)
    return out


def total_cost(cost: list[list[int]], assignment: list[int]) -> int:
    return sum(cost[i][j] for i, j in enumerate(assignment) if j != -1)


# --------------------------------------------------------------------------
# Entry point used by the matcher
# --------------------------------------------------------------------------

def assign(bank_rows, settlements) -> list[Pairing]:
    """
    Pair a set of ambiguous bank rows with a set of candidate settlements.

    Returns one Pairing per bank row. Inadmissible pairings are returned marked
    as such rather than silently dropped, so the caller can escalate them with
    a stated reason instead of reporting an unmatched row with no explanation.
    """
    if not bank_rows or not settlements:
        return []

    cost, reasons = [], []
    for row in bank_rows:
        row_costs, row_reasons = [], []
        for s in settlements:
            c, why = pair_cost(row.movement_paise, row.value_date,
                               s.total_paise, s.capture_date)
            row_costs.append(c)
            row_reasons.append(why)
        cost.append(row_costs)
        reasons.append(row_reasons)

    assignment = hungarian(cost)

    out = []
    for i, j in enumerate(assignment):
        if j == -1:
            out.append(Pairing(i, -1, IMPOSSIBLE, False,
                               "no settlement available for this row"))
            continue
        admissible = cost[i][j] < IMPOSSIBLE
        out.append(Pairing(i, j, cost[i][j], admissible, reasons[i][j]))
    return out
