"""
Tests for reconciliation behaviour: the matcher, the verification gate, the
subset-sum bound, and the agent's enforced constraints.

The agent tests run without a language model. Every guard is enforced in code,
so every guard can be tested by calling that code directly with adversarial
input. A constraint that can only be tested by hoping the model behaves is not
a constraint.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent import CLASSIFICATIONS, AgentResult, ResolutionAgent  # noqa: E402
from core import paise_to_rupees_str, rupees_to_paise  # noqa: E402
from llm import FallbackChain, LLMResponse, Provider, classify_failure  # noqa: E402
from matcher import Engine, load  # noqa: E402
from narration import Proposal, propose_by_regex, verify  # noqa: E402
from tools import InvestigationTools, subset_sum  # noqa: E402


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    """A generated batch with a known answer key, built once per test run."""
    out = tmp_path_factory.mktemp("batch")
    subprocess.run(
        [sys.executable, str(ROOT / "src" / "generate_data.py"),
         "--seed", "42", "--orders", "120", "--out", str(out)],
        check=True, capture_output=True)
    return out


@pytest.fixture(scope="module")
def loaded(batch):
    return load(batch)


# --------------------------------------------------------------------------
# Matcher
# --------------------------------------------------------------------------

class TestMatcher:

    def test_every_entity_is_accounted_for(self, loaded):
        """
        The property that matters most: nothing is silently dropped. Every
        entity the engine examines appears in the output exactly once.
        """
        orders, txns, settlements, bank = loaded
        resolutions = Engine(orders, txns, settlements, bank).run()
        ids = [r.entity_id for r in resolutions]
        assert len(ids) == len(set(ids)), "an entity was reported twice"
        assert len(resolutions) > 0

    def test_unresolved_records_always_carry_a_reason(self, loaded):
        orders, txns, settlements, bank = loaded
        for r in Engine(orders, txns, settlements, bank).run():
            if not r.resolved:
                assert r.detail, f"{r.entity_id} escalated with no reason"

    def test_every_resolution_records_its_tier(self, loaded):
        orders, txns, settlements, bank = loaded
        for r in Engine(orders, txns, settlements, bank).run():
            assert r.tier in (0, 1, 2, 3)

    def test_unsettled_rows_are_not_treated_as_errors(self, loaded):
        """
        A capture near the end of the window has not been paid out yet. That is
        normal. Conflating it with a genuine break would fill the exception
        list with rows behaving correctly.
        """
        orders, txns, settlements, bank = loaded
        unsettled = [r for r in Engine(orders, txns, settlements, bank).run()
                     if r.classification == "unsettled"]
        assert unsettled, "fixture should contain unsettled rows"
        assert all(r.resolved for r in unsettled)

    def test_rounding_noise_is_tolerated_not_reported(self, loaded):
        orders, txns, settlements, bank = loaded
        noise = [r for r in Engine(orders, txns, settlements, bank).run()
                 if r.classification == "rounding_noise"]
        assert noise, "fixture should contain sub-paise drift"
        assert all(r.resolved for r in noise)

    def test_real_breaks_are_never_marked_resolved(self, loaded):
        """Money missing or unaccounted for must reach a human."""
        orders, txns, settlements, bank = loaded
        breaks = {"missing_payment", "orphan_bank_credit", "missing_bank_row",
                  "settlement_not_in_bank"}
        for r in Engine(orders, txns, settlements, bank).run():
            if r.classification in breaks:
                assert not r.resolved, f"{r.entity_id} silently resolved"

    def test_run_is_deterministic(self, loaded):
        orders, txns, settlements, bank = loaded
        a = Engine(orders, txns, settlements, bank).run()
        b = Engine(orders, txns, settlements, bank).run()
        assert [(r.entity_id, r.classification, r.tier) for r in a] == \
               [(r.entity_id, r.classification, r.tier) for r in b]

    def test_bank_movement_handles_debits(self, loaded):
        """
        A settlement is net negative on a refund-heavy day and appears as a
        debit. Reading credits alone reported every such day as missing -- the
        first real bug in this project.
        """
        _, _, _, bank = loaded
        for row in bank:
            expected = (row.credit_paise or 0) - (row.debit_paise or 0)
            assert row.movement_paise == expected


# --------------------------------------------------------------------------
# Verification gate
# --------------------------------------------------------------------------

class _FakeSettlement:
    def __init__(self, sid, total, utr, capture):
        self.settlement_id, self.total_paise = sid, total
        self.utr, self.capture_date = utr, capture


class TestVerificationGate:

    @pytest.fixture
    def settlements(self):
        return [_FakeSettlement("setl_0001", 45000, "RZP123456789",
                                date(2026, 8, 10))]

    def test_accepts_only_when_every_check_passes(self, settlements):
        r = verify(Proposal("RZP123456789", "exact", "exact"),
                   45000, date(2026, 8, 11), settlements)
        assert r.accepted
        assert len(r.checks_passed) == 3

    def test_rejects_a_reference_that_resolves_to_nothing(self, settlements):
        """
        The case that motivated the gate. A well-formed reference extracted
        cleanly by regex, corresponding to no settlement at all.
        """
        r = verify(Proposal("RZP999999999", "exact", "exact"),
                   45000, date(2026, 8, 11), settlements)
        assert not r.accepted
        assert "0 settlements" in r.reason

    def test_rejects_when_the_amount_does_not_tie(self, settlements):
        r = verify(Proposal("RZP123456789", "exact", "exact"),
                   45001, date(2026, 8, 11), settlements)
        assert not r.accepted
        assert "does not tie" in r.reason

    def test_rejects_when_the_date_is_outside_the_window(self, settlements):
        r = verify(Proposal("RZP123456789", "exact", "exact"),
                   45000, date(2026, 9, 30), settlements)
        assert not r.accepted
        assert "outside" in r.reason

    def test_rejects_a_truncated_prefix_that_is_ambiguous(self):
        two = [_FakeSettlement("a", 45000, "RZP111000001", date(2026, 8, 10)),
               _FakeSettlement("b", 45000, "RZP111000002", date(2026, 8, 10))]
        r = verify(Proposal("RZP1110", "truncated", "reconstructed"),
                   45000, date(2026, 8, 11), two)
        assert not r.accepted
        assert "not unique" in r.reason

    def test_accepts_a_truncated_prefix_that_completes_uniquely(self,
                                                               settlements):
        r = verify(Proposal("RZP1234", "truncated", "reconstructed"),
                   45000, date(2026, 8, 11), settlements)
        assert r.accepted

    def test_no_proposal_is_not_an_acceptance(self, settlements):
        r = verify(Proposal(None, "none", "none"), 45000,
                   date(2026, 8, 11), settlements)
        assert not r.accepted


class TestNarrationExtraction:

    @pytest.mark.parametrize("text,expected", [
        ("NEFT CR-HDFC0000123-RAZORPAY SOFTWARE PVT LTD-RZP123456789",
         "RZP123456789"),
        ("NEFT-RZP123456789-RAZORPAY SOFTWARE PRIVATE LIMITED",
         "RZP123456789"),
        ("IMPS/RZP123456789/RAZORPAY/SETTLEMENT", "RZP123456789"),
    ])
    def test_clean_narrations_extract_exactly(self, text, expected):
        assert propose_by_regex(text).utr == expected

    def test_absent_reference_proposes_nothing(self):
        p = propose_by_regex("TRF FROM RAZORPAY SOFTWARE PVT LTD")
        assert p.utr is None
        assert p.confidence == "none"

    def test_truncated_reference_is_a_prefix_not_an_identifier(self):
        """
        A partial string must not be treated as a match. It is returned as a
        prefix so the verifier can attempt unique completion, or refuse.
        """
        p = propose_by_regex("NEFT CR RAZORPAY SOFTWA RZP12345")
        assert p.confidence == "reconstructed"
        assert len(p.utr) < 12


# --------------------------------------------------------------------------
# Subset sum
# --------------------------------------------------------------------------

class TestSubsetSum:

    def test_finds_an_exact_subset(self):
        idx = subset_sum([100, 250, 375, 500], 625, max_terms=2)
        assert idx is not None
        assert sum([100, 250, 375, 500][i] for i in idx) == 625

    def test_returns_none_rather_than_an_approximation(self):
        assert subset_sum([100, 250, 375], 999, max_terms=3) is None

    def test_handles_signed_values(self):
        """Refunds are negative. They must participate naturally."""
        idx = subset_sum([1000, -300, 500], 700, max_terms=2)
        assert idx is not None

    def test_respects_the_term_bound(self):
        """
        Four values are needed; a bound of three must refuse rather than
        silently widening. The bound is evidential, not only computational:
        coincidental exact sums become common as term count rises.
        """
        values = [100, 200, 300, 400]
        assert subset_sum(values, 1000, max_terms=3) is None
        assert subset_sum(values, 1000, max_terms=4) is not None


# --------------------------------------------------------------------------
# Agent guards -- no model involved
# --------------------------------------------------------------------------

class TestAgentGuards:
    """
    Every constraint is enforced in code, so every constraint is testable
    without a language model. These call the verdict parser directly with the
    output a misbehaving model would produce.
    """

    def test_classification_outside_the_taxonomy_is_downgraded(self):
        r = AgentResult("X", "order")
        ResolutionAgent._parse_verdict(
            '{"classification":"definitely_fine","resolved":true}', r)
        assert r.classification == "unexplained"
        assert not r.resolved
        assert "outside the taxonomy" in r.analyst_note

    def test_resolution_without_evidence_is_overruled(self):
        r = AgentResult("Y", "order")           # no steps recorded
        ResolutionAgent._parse_verdict(
            '{"classification":"refund","resolved":true}', r)
        assert r.classification == "refund"
        assert not r.resolved, "claimed a resolution with no tool call"

    def test_unparseable_output_escalates(self):
        r = AgentResult("Z", "order")
        ResolutionAgent._parse_verdict("I think this is probably fine.", r)
        assert r.classification == "unexplained"
        assert not r.resolved

    def test_malformed_json_escalates(self):
        r = AgentResult("W", "order")
        ResolutionAgent._parse_verdict('{"classification": "refund"', r)
        assert r.classification == "unexplained"

    def test_fenced_json_is_still_parsed(self):
        r = AgentResult("V", "order")
        r.steps.append(object())                # evidence exists
        ResolutionAgent._parse_verdict(
            '```json\n{"classification":"refund","resolved":true}\n```', r)
        assert r.classification == "refund"
        assert r.resolved

    def test_taxonomy_has_no_duplicates(self):
        assert len(CLASSIFICATIONS) == len(set(CLASSIFICATIONS))

    def test_tool_registry_is_closed(self, loaded):
        """The agent cannot widen its own capabilities."""
        orders, txns, settlements, bank = loaded
        agent = ResolutionAgent(InvestigationTools(orders, txns, settlements,
                                                   bank))
        assert "delete_all_records" not in agent.dispatch
        assert agent.dispatch.get("execute_sql") is None


# --------------------------------------------------------------------------
# Provider failover
# --------------------------------------------------------------------------

class _Stub(Provider):
    def __init__(self, name, candidates, fails):
        self.name, self.candidates, self.fails = name, candidates, fails
        self.available, self.tried = True, []

    def complete(self, system, messages, tools=None, max_tokens=1024,
                 model=None):
        self.tried.append(model)
        if model in self.fails:
            return LLMResponse(provider=self.name, error=self.fails[model])
        return LLMResponse(text=f"ok from {self.name}:{model}",
                           provider=self.name)

    def assistant_turn(self, resp): return {}
    def tool_results_turn(self, results): return {}


class TestFailover:

    @pytest.mark.parametrize("error,kind", [
        ("Your credit balance is too low", "provider"),
        ("Error 401 invalid api key", "provider"),
        ("rate limit quota exceeded", "provider"),
        ("The model `llama-3.3-70b` does not exist", "model"),
        ("404 model_not_found", "model"),
        ("connection reset by peer", "model"),
    ])
    def test_failures_are_classified_by_remedy(self, error, kind):
        assert classify_failure(error) == kind

    def test_a_retired_model_does_not_cost_the_provider(self):
        """
        The bug this design corrects. A valid key with one stale model name
        previously discarded an otherwise working provider.
        """
        p = _Stub("groq", ["gpt-oss-120b", "gpt-oss-20b"],
                  {"gpt-oss-120b": "404 The model does not exist"})
        chain = FallbackChain([p])
        resp = chain.complete("s", [{"role": "user", "content": "x"}])
        assert resp.ok
        assert p.tried == ["gpt-oss-120b", "gpt-oss-20b"]

    def test_a_dead_account_skips_its_remaining_models(self):
        p = _Stub("anthropic", ["sonnet", "haiku"],
                  {"sonnet": "400 Your credit balance is too low",
                   "haiku": "400 Your credit balance is too low"})
        alive = _Stub("groq", ["gpt-oss-120b"], {})
        chain = FallbackChain([p, alive])
        resp = chain.complete("s", [{"role": "user", "content": "x"}])
        assert resp.ok and resp.provider == "groq"
        assert p.tried == ["sonnet"], "wasted a call on a dead account"

    def test_dead_candidates_are_not_retried(self):
        dead = _Stub("a", ["m1"], {"m1": "404 does not exist"})
        alive = _Stub("b", ["m2"], {})
        chain = FallbackChain([dead, alive])
        for _ in range(3):
            chain.complete("s", [{"role": "user", "content": "x"}])
        assert dead.tried == ["m1"], "retried a known-dead candidate"

    def test_total_failure_reports_rather_than_raising(self):
        chain = FallbackChain([_Stub("a", ["m"], {"m": "404 gone"})])
        resp = chain.complete("s", [{"role": "user", "content": "x"}])
        assert not resp.ok
        assert resp.error

    def test_no_answering_model_is_claimed_before_one_answers(self):
        chain = FallbackChain([_Stub("a", ["m"], {"m": "404 gone"})])
        chain.complete("s", [{"role": "user", "content": "x"}])
        assert chain.active is None
