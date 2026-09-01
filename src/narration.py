"""
Tier 3: recovering a settlement reference from unstructured bank narration.

This is the only place in the system where a language model is used, and it is
deliberately confined to the one task deterministic code does badly: reading
inconsistent free text written by humans and legacy banking systems.

Architecture:

    PROPOSE   regex, then a language model on what regex cannot parse
    VERIFY    deterministic: does the candidate exist, does the amount tie,
              is the date inside the plausible payout window
    ACCEPT    only if every verification passes

The model never decides a match. It can only nominate a candidate, which is
then checked against arithmetic the model has no influence over. The system
therefore cannot emit an incorrect match no matter how badly the model behaves
-- a wrong proposal becomes an exception, not a wrong number in the books.

Run with --no-verify to demonstrate what the verification gate prevents.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import paise_to_rupees_str, working_day_window  # noqa: E402
from llm import get_provider  # noqa: E402


# --------------------------------------------------------------------------
# Stage A: deterministic extraction
# --------------------------------------------------------------------------
# Handles the clean and near-clean majority. Ordered most specific first.
# Each pattern is separately named so the report can show WHICH rule fired,
# rather than presenting extraction as an opaque step.

UTR_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("exact",     re.compile(r"\b(RZP\d{9})\b")),
    ("spaced",    re.compile(r"\b(RZP[\s]?\d{3}[\s]?\d{4}[\s]?\d{2})\b")),
    ("embedded",  re.compile(r"[-/](RZP\d{9})[-/]")),
]

TRUNCATED_PATTERN = re.compile(r"\b(RZP\d{4,8})\b")


@dataclass
class Proposal:
    """A candidate reference and where it came from. Never a decision."""
    utr: str | None
    source: str               # regex rule name, "llm", or "none"
    confidence: str           # exact | reconstructed | inferred | none
    raw_span: str = ""


def propose_by_regex(description: str) -> Proposal:
    for name, pattern in UTR_PATTERNS:
        m = pattern.search(description)
        if m:
            candidate = re.sub(r"\s+", "", m.group(1))
            if len(candidate) == 12:          # RZP + 9 digits
                return Proposal(candidate, name, "exact", m.group(0))

    m = TRUNCATED_PATTERN.search(description)
    if m:
        # A truncated reference is a prefix, not an identifier. Return it as
        # a prefix so the verifier can attempt a unique completion rather
        # than treating a partial string as a match.
        return Proposal(m.group(1), "truncated", "reconstructed", m.group(0))

    return Proposal(None, "none", "none")


# --------------------------------------------------------------------------
# Stage B: language model proposal
# --------------------------------------------------------------------------
# Used only where regex found nothing usable. The prompt is deliberately
# narrow: extract a token, do not reason about matching, do not invent.
#
# If no API key is configured the module degrades to deterministic-only
# operation and reports that it did so. The system must remain runnable and
# honest without network access; a demo that silently changes behaviour when
# a key is missing is worse than one that says so.

EXTRACTION_PROMPT = """You are reading a single line of narration from an \
Indian bank statement. A settlement reference may be present, mangled, \
abbreviated, or entirely absent.

Return ONLY the reference token if one is present, with no explanation and no \
punctuation. Return exactly NONE if no reference is present.

Do not invent a reference. Do not complete a partial one. Do not guess.

Narration: {description}"""


class LLMProposer:
    def __init__(self, provider=None) -> None:
        self.provider = provider or get_provider()
        self.available = self.provider.available
        self.calls = 0
        self.reason_unavailable = ("" if self.available
                                   else getattr(self.provider, "reason",
                                                "no provider configured"))

    def propose(self, description: str) -> Proposal:
        if not self.available:
            return Proposal(None, "llm_unavailable", "none")

        self.calls += 1
        resp = self.provider.complete(
            system="You extract identifiers from bank statement narration.",
            messages=[{"role": "user",
                       "content": EXTRACTION_PROMPT.format(
                           description=description)}],
            max_tokens=64)

        if not resp.ok:
            # A model failure must never fail the reconciliation run. The row
            # falls through to the exception list, which is the correct
            # outcome for something the system could not resolve.
            return Proposal(None, "llm_error", "none", raw_span=resp.error[:80])
        text = resp.text.strip()

        if not text or text.upper() == "NONE":
            return Proposal(None, "llm", "none")
        return Proposal(re.sub(r"\s+", "", text), "llm", "inferred", text)


# --------------------------------------------------------------------------
# Stage C: verification gate
# --------------------------------------------------------------------------
# Nothing above this line is trusted. A proposal is accepted only if it
# survives every check, and each check is arithmetic the proposer cannot
# influence.

@dataclass
class VerificationResult:
    accepted: bool
    settlement_id: str = ""
    reason: str = ""
    checks_passed: list[str] = None

    def __post_init__(self):
        if self.checks_passed is None:
            self.checks_passed = []


def verify(proposal: Proposal, movement_paise: int, value_date: date,
           settlements: list) -> VerificationResult:
    """
    Accept a proposed reference only if:
      1. it resolves to exactly one known settlement
      2. the settlement total equals the bank movement exactly
      3. the bank date falls inside the plausible payout window

    Any failure returns the row to the exception list.
    """
    if proposal.utr is None:
        return VerificationResult(False, reason="no reference proposed")

    passed: list[str] = []

    if proposal.confidence == "reconstructed":
        # A truncated prefix is only usable if it completes uniquely.
        candidates = [s for s in settlements
                      if s.utr and s.utr.startswith(proposal.utr)]
        if len(candidates) != 1:
            return VerificationResult(
                False,
                reason=(f"truncated reference '{proposal.utr}' matches "
                        f"{len(candidates)} settlements; not unique"))
        passed.append("unique prefix completion")
        settlement = candidates[0]
    else:
        exact = [s for s in settlements if s.utr == proposal.utr]
        if len(exact) != 1:
            return VerificationResult(
                False,
                reason=(f"reference '{proposal.utr}' resolves to "
                        f"{len(exact)} settlements"))
        passed.append("reference exists")
        settlement = exact[0]

    if settlement.total_paise != movement_paise:
        return VerificationResult(
            False,
            reason=(f"reference resolves to {settlement.settlement_id} but "
                    f"amount {paise_to_rupees_str(settlement.total_paise)} "
                    f"does not tie to bank movement "
                    f"{paise_to_rupees_str(movement_paise)}"))
    passed.append("amount ties exactly")

    lo, hi = working_day_window(settlement.capture_date)
    if not (lo <= value_date <= hi):
        return VerificationResult(
            False,
            reason=(f"date {value_date} outside plausible payout window "
                    f"{lo}..{hi}"))
    passed.append("date inside payout window")

    return VerificationResult(True, settlement.settlement_id,
                              "all checks passed", passed)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

@dataclass
class Tier3Outcome:
    bank_txn_id: str
    proposal: Proposal
    verification: VerificationResult


class Tier3Resolver:
    def __init__(self, use_llm: bool = True, verify_gate: bool = True):
        self.proposer = LLMProposer() if use_llm else None
        self.verify_gate = verify_gate
        self.stats = {"regex": 0, "llm": 0, "rejected": 0, "accepted": 0}

    def resolve(self, bank_row, settlements) -> Tier3Outcome:
        proposal = propose_by_regex(bank_row.description)

        if proposal.utr is None and self.proposer is not None:
            proposal = self.proposer.propose(bank_row.description)
            if proposal.utr is not None:
                self.stats["llm"] += 1
        elif proposal.utr is not None:
            self.stats["regex"] += 1

        if not self.verify_gate:
            # Ablation path. Accepts the proposal on trust, which is how a
            # system without a verification gate behaves. Present so the
            # failure mode can be demonstrated rather than described.
            accepted = proposal.utr is not None
            match = next((s for s in settlements if s.utr and
                          s.utr.startswith(proposal.utr)), None) if accepted else None
            result = VerificationResult(
                accepted, match.settlement_id if match else "UNKNOWN",
                "VERIFICATION DISABLED - accepted on trust")
        else:
            result = verify(proposal, bank_row.movement_paise,
                            bank_row.value_date, settlements)

        if result.accepted:
            self.stats["accepted"] += 1
        elif proposal.utr is not None:
            self.stats["rejected"] += 1

        return Tier3Outcome(bank_row.bank_txn_id, proposal, result)
