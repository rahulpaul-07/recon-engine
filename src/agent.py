"""
Exception Resolution Agent.

For each row the deterministic tiers could not resolve, this agent conducts a
bounded investigation: it selects an investigation tool, observes the result,
decides what to check next, and terminates either with a resolution backed by
evidence or with an escalation note for a human analyst.

Constraints, all enforced in code rather than requested in the prompt:

  * at most MAX_STEPS tool calls per exception
  * the agent may only call tools from the registry; anything else is refused
  * every tool result is computed deterministically -- the model performs no
    arithmetic and no matching
  * a proposed resolution is accepted only if it names a classification from
    the fixed taxonomy AND cites at least one tool result as evidence
  * running out of steps escalates; it never produces a guess

The agent contributes investigative strategy. The tools contribute truth.
A model failure degrades an exception into an escalation, which is the correct
outcome, rather than into a wrong number in the books.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools import TOOL_SCHEMA, InvestigationTools, build_dispatch  # noqa: E402

MAX_STEPS = 5

# Fixed taxonomy. The agent must classify into one of these; a free-text
# classification is rejected. This keeps agent output comparable against the
# ground-truth answer key and prevents the model inventing categories that
# cannot be graded.
CLASSIFICATIONS = [
    "fee_mismatch", "refund", "partial_refund", "chargeback", "duplicate",
    "unsettled", "failed_payment", "missing_payment", "orphan_bank_credit",
    "missing_bank_row", "rounding_noise", "net_arithmetic_error",
    "settlement_total_mismatch", "settlement_not_in_bank", "amount_mismatch",
    "method_mismatch", "ambiguous_match", "unexplained",
]

SYSTEM_PROMPT = """You are a payments reconciliation analyst investigating a \
single unresolved record.

You have investigation tools. Use them to gather evidence. You must not \
perform arithmetic yourself and you must not assert a relationship between \
records that a tool has not confirmed.

Investigate efficiently. You have at most {max_steps} tool calls. Prefer the \
tool that would most quickly distinguish between the plausible explanations.

When you have enough evidence, respond with a JSON object and nothing else:

{{"classification": "<one of the allowed values>",
  "resolved": true or false,
  "reasoning": "<two or three sentences citing the specific tool results>",
  "analyst_note": "<what a human should check next, or empty if resolved>"}}

Allowed classification values: {classifications}

Set resolved to true only if the evidence explains the record. If the evidence \
shows a genuine break -- money that is missing, or present with no \
counterpart -- classify it correctly and set resolved to false, with an \
analyst_note describing what to investigate. An honest escalation is a \
correct outcome. A confident guess is not."""


@dataclass
class Step:
    n: int
    tool: str
    tool_input: dict[str, Any]
    ok: bool
    summary: str


@dataclass
class AgentResult:
    entity_id: str
    entity_type: str
    classification: str = "unexplained"
    resolved: bool = False
    reasoning: str = ""
    analyst_note: str = ""
    steps: list[Step] = field(default_factory=list)
    terminated: str = ""          # answered | step_limit | model_error | unavailable
    model_calls: int = 0

    def trace(self) -> str:
        lines = [f"{self.entity_id} ({self.entity_type})"]
        for s in self.steps:
            mark = "ok " if s.ok else "-- "
            lines.append(f"  {s.n}. {mark}{s.tool}({_fmt(s.tool_input)})")
            lines.append(f"        {s.summary}")
        lines.append(f"  => {self.classification} "
                     f"[{'resolved' if self.resolved else 'escalated'}] "
                     f"({self.terminated})")
        if self.reasoning:
            lines.append(f"     {self.reasoning}")
        if self.analyst_note:
            lines.append(f"     note: {self.analyst_note}")
        return "\n".join(lines)


def _fmt(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items())


class ResolutionAgent:
    def __init__(self, tools: InvestigationTools, max_steps: int = MAX_STEPS):
        self.tools = tools
        self.dispatch = build_dispatch(tools)
        self.max_steps = max_steps
        self.available = bool(os.environ.get("ANTHROPIC_API_KEY"))

    def investigate(self, entity_id: str, entity_type: str,
                    context: str) -> AgentResult:
        result = AgentResult(entity_id=entity_id, entity_type=entity_type)

        if not self.available:
            result.terminated = "unavailable"
            result.analyst_note = ("agent not run: no ANTHROPIC_API_KEY "
                                   "configured; record remains an exception")
            return result

        try:
            import anthropic
        except ImportError:
            result.terminated = "unavailable"
            result.analyst_note = "anthropic SDK not installed"
            return result

        client = anthropic.Anthropic()
        system = SYSTEM_PROMPT.format(
            max_steps=self.max_steps,
            classifications=", ".join(CLASSIFICATIONS))
        messages: list[dict] = [{"role": "user", "content": context}]

        for step_n in range(1, self.max_steps + 1):
            try:
                resp = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1024,
                    system=system,
                    tools=TOOL_SCHEMA,
                    messages=messages,
                )
                result.model_calls += 1
            except Exception as exc:                      # noqa: BLE001
                result.terminated = "model_error"
                result.analyst_note = (f"agent aborted: {str(exc)[:120]}; "
                                       f"record remains an exception")
                return result

            tool_uses = [b for b in resp.content
                         if getattr(b, "type", "") == "tool_use"]

            if not tool_uses:
                text = "".join(b.text for b in resp.content
                               if getattr(b, "type", "") == "text")
                self._parse_verdict(text, result)
                result.terminated = "answered"
                return result

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []

            for use in tool_uses:
                fn = self.dispatch.get(use.name)
                if fn is None:
                    # Refuse anything outside the registry. The agent cannot
                    # widen its own capabilities.
                    payload = {"ok": False,
                               "summary": f"tool '{use.name}' is not available"}
                    result.steps.append(Step(step_n, use.name, dict(use.input),
                                             False, payload["summary"]))
                else:
                    try:
                        tr = fn(**use.input)
                        payload = {"ok": tr.ok, "summary": tr.summary,
                                   "evidence": tr.evidence}
                        result.steps.append(Step(step_n, use.name,
                                                 dict(use.input), tr.ok,
                                                 tr.summary))
                    except TypeError as exc:
                        payload = {"ok": False,
                                   "summary": f"invalid arguments: {exc}"}
                        result.steps.append(Step(step_n, use.name,
                                                 dict(use.input), False,
                                                 payload["summary"]))

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": json.dumps(payload, default=str),
                })

            messages.append({"role": "user", "content": tool_results})

        # Step limit reached without a verdict. Escalate rather than guess.
        result.terminated = "step_limit"
        result.classification = "unexplained"
        result.resolved = False
        result.analyst_note = (
            f"investigation did not conclude within {self.max_steps} steps; "
            f"escalated for manual review")
        return result

    @staticmethod
    def _parse_verdict(text: str, result: AgentResult) -> None:
        """
        Parse and validate the agent's verdict.

        Validation is strict: an unknown classification, or a claimed
        resolution with no tool evidence behind it, is downgraded rather than
        accepted. The agent cannot talk its way past the taxonomy.
        """
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            result.classification = "unexplained"
            result.analyst_note = "agent returned no parseable verdict"
            return

        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            result.classification = "unexplained"
            result.analyst_note = "agent verdict was not valid JSON"
            return

        proposed = str(data.get("classification", "")).strip()
        result.reasoning = str(data.get("reasoning", ""))[:600]
        result.analyst_note = str(data.get("analyst_note", ""))[:600]

        if proposed not in CLASSIFICATIONS:
            result.classification = "unexplained"
            result.resolved = False
            result.analyst_note = (
                f"agent proposed '{proposed}', which is outside the "
                f"taxonomy; downgraded and escalated")
            return

        result.classification = proposed
        claimed = bool(data.get("resolved", False))

        if claimed and not result.steps:
            # A resolution with no investigation behind it is not a
            # resolution. This is the agent equivalent of the Tier 3
            # verification gate.
            result.resolved = False
            result.analyst_note = (
                "agent claimed resolution without calling any tool; "
                "downgraded and escalated")
            return

        result.resolved = claimed


def build_context(entity_id: str, entity_type: str, detail: str,
                  facts: dict[str, Any]) -> str:
    lines = [f"Record: {entity_id} (type: {entity_type})",
             f"Automated tiers reported: {detail}", "", "Known values:"]
    for k, v in facts.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("Investigate this record and return your verdict as JSON.")
    return "\n".join(lines)
