"""
Provider-agnostic language model interface.

The reconciliation engine must not depend on a single vendor being reachable.
This module presents one interface over several providers and one honest
fallback, so the system degrades rather than failing.

Providers, tried in order unless one is named explicitly:

    anthropic   native tool use, what the agent was designed against
    groq        OpenAI-compatible tool calling, free tier
    gemini      Google function calling, free tier
    none        no model available; deterministic paths only

Selection is by environment variable. Setting any one of these is enough:

    ANTHROPIC_API_KEY
    GROQ_API_KEY
    GEMINI_API_KEY

Override the choice with RECON_LLM_PROVIDER=groq (or anthropic / gemini / none).

Design stance
-------------
Tool schemas are written once, in Anthropic's format, and translated per
provider. Translating at the boundary keeps one source of truth for what the
tools are; duplicating the schema per vendor would let them drift apart
silently, which is exactly the class of bug this project exists to catch.

`none` is a first-class provider, not an error state. Every caller must handle
an unavailable model, because in production a provider outage is a Tuesday,
not an exception.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Normalised response
# --------------------------------------------------------------------------

@dataclass
class ToolCall:
    """One tool invocation requested by the model."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """
    Provider-neutral response.

    `text` and `tool_calls` may both be populated: some providers narrate
    before calling a tool. Callers should treat a non-empty `tool_calls` as
    "the model wants to act" regardless of accompanying text.
    """
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    provider: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------

class Provider:
    name = "base"
    available = False

    def complete(self, system: str, messages: list[dict],
                 tools: list[dict] | None = None,
                 max_tokens: int = 1024) -> LLMResponse:
        raise NotImplementedError

    # Conversation records are kept in Anthropic's shape internally, because
    # that is the richest of the three. Each provider adapts on the way out.
    @staticmethod
    def assistant_turn(resp: LLMResponse) -> dict:
        raise NotImplementedError

    @staticmethod
    def tool_results_turn(results: list[tuple[str, dict]]) -> dict:
        raise NotImplementedError


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------

class AnthropicProvider(Provider):
    name = "anthropic"
    default_model = "claude-sonnet-4-6"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("RECON_LLM_MODEL",
                                             self.default_model)
        self.available = False
        self._client = None
        if not os.environ.get("ANTHROPIC_API_KEY"):
            self.reason = "ANTHROPIC_API_KEY not set"
            return
        try:
            import anthropic
            self._client = anthropic.Anthropic()
            self.available = True
            self.reason = ""
        except ImportError:
            self.reason = "anthropic SDK not installed (pip install anthropic)"

    def complete(self, system, messages, tools=None, max_tokens=1024):
        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
            resp = self._client.messages.create(**kwargs)
        except Exception as exc:                          # noqa: BLE001
            return LLMResponse(provider=self.name, error=str(exc)[:200])

        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content
                 if getattr(b, "type", "") == "tool_use"]
        return LLMResponse(text=text, tool_calls=calls, raw=resp.content,
                           provider=self.name)

    @staticmethod
    def assistant_turn(resp):
        return {"role": "assistant", "content": resp.raw}

    @staticmethod
    def tool_results_turn(results):
        return {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": cid,
             "content": json.dumps(payload, default=str)}
            for cid, payload in results]}


# --------------------------------------------------------------------------
# Groq (OpenAI-compatible)
# --------------------------------------------------------------------------

class GroqProvider(Provider):
    name = "groq"
    default_model = "llama-3.3-70b-versatile"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("RECON_LLM_MODEL",
                                             self.default_model)
        self.available = False
        self._client = None
        if not os.environ.get("GROQ_API_KEY"):
            self.reason = "GROQ_API_KEY not set"
            return
        try:
            from groq import Groq
            self._client = Groq()
            self.available = True
            self.reason = ""
        except ImportError:
            self.reason = "groq SDK not installed (pip install groq)"

    @staticmethod
    def _translate_tools(tools: list[dict]) -> list[dict]:
        """Anthropic tool schema -> OpenAI function schema."""
        return [{"type": "function",
                 "function": {"name": t["name"],
                              "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in tools]

    @staticmethod
    def _translate_messages(system: str, messages: list[dict]) -> list[dict]:
        """
        Internal (Anthropic-shaped) history -> OpenAI chat format.

        Anthropic carries tool results as content blocks inside a user turn;
        OpenAI wants a separate message per result with role "tool". This is
        the only structurally awkward part of the translation.
        """
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            content = m["content"]
            if isinstance(content, str):
                out.append({"role": m["role"], "content": content})
                continue
            if m["role"] == "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out.append({"role": "tool",
                                    "tool_call_id": block["tool_use_id"],
                                    "content": block["content"]})
                    elif isinstance(block, dict) and block.get("type") == "text":
                        out.append({"role": "user", "content": block["text"]})
            else:
                out.append(m)
        return out

    def complete(self, system, messages, tools=None, max_tokens=1024):
        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": self._translate_messages(system, messages),
            }
            if tools:
                kwargs["tools"] = self._translate_tools(tools)
                kwargs["tool_choice"] = "auto"
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:                          # noqa: BLE001
            return LLMResponse(provider=self.name, error=str(exc)[:200])

        msg = resp.choices[0].message
        calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        return LLMResponse(text=msg.content or "", tool_calls=calls,
                           raw=msg, provider=self.name)

    @staticmethod
    def assistant_turn(resp):
        # Re-expressed in the internal Anthropic shape so the agent loop does
        # not need to know which provider produced it.
        blocks: list[dict] = []
        if resp.text:
            blocks.append({"type": "text", "text": resp.text})
        for c in resp.tool_calls:
            blocks.append({"type": "tool_use", "id": c.id,
                           "name": c.name, "input": c.arguments})
        return {"role": "assistant", "content": blocks}

    @staticmethod
    def tool_results_turn(results):
        return AnthropicProvider.tool_results_turn(results)


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------

class GeminiProvider(Provider):
    name = "gemini"
    default_model = "gemini-2.0-flash"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("RECON_LLM_MODEL",
                                             self.default_model)
        self.available = False
        self._client = None
        if not os.environ.get("GEMINI_API_KEY"):
            self.reason = "GEMINI_API_KEY not set"
            return
        try:
            from google import genai
            self._client = genai.Client()
            self.available = True
            self.reason = ""
        except ImportError:
            self.reason = ("google-genai SDK not installed "
                           "(pip install google-genai)")

    @staticmethod
    def _translate_tools(tools: list[dict]) -> list[dict]:
        return [{"function_declarations": [
            {"name": t["name"], "description": t["description"],
             "parameters": t["input_schema"]} for t in tools]}]

    @staticmethod
    def _flatten(system: str, messages: list[dict]) -> str:
        """
        Gemini's multi-turn tool format differs enough that, for the short
        bounded loops this project runs, flattening the exchange into a single
        prompt is more reliable than a partial translation. Stated plainly
        because it is a limitation, not a design preference.
        """
        parts = [system, ""]
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                parts.append(f"{m['role']}: {c}")
            else:
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        parts.append(f"{m['role']}: {b['text']}")
                    elif b.get("type") == "tool_use":
                        parts.append(f"assistant called {b['name']}"
                                     f"({json.dumps(b['input'])})")
                    elif b.get("type") == "tool_result":
                        parts.append(f"tool returned: {b['content']}")
        return "\n".join(parts)

    def complete(self, system, messages, tools=None, max_tokens=1024):
        try:
            from google.genai import types
            cfg: dict[str, Any] = {"max_output_tokens": max_tokens}
            if tools:
                cfg["tools"] = [types.Tool(function_declarations=[
                    types.FunctionDeclaration(
                        name=t["name"], description=t["description"],
                        parameters=t["input_schema"]) for t in tools])]
            resp = self._client.models.generate_content(
                model=self.model,
                contents=self._flatten(system, messages),
                config=types.GenerateContentConfig(**cfg))
        except Exception as exc:                          # noqa: BLE001
            return LLMResponse(provider=self.name, error=str(exc)[:200])

        text, calls = "", []
        for i, part in enumerate(
                getattr(resp.candidates[0].content, "parts", []) or []):
            if getattr(part, "text", None):
                text += part.text
            fc = getattr(part, "function_call", None)
            if fc:
                calls.append(ToolCall(f"gemini_{i}", fc.name,
                                      dict(fc.args or {})))
        return LLMResponse(text=text, tool_calls=calls, raw=resp,
                           provider=self.name)

    @staticmethod
    def assistant_turn(resp):
        return GroqProvider.assistant_turn(resp)

    @staticmethod
    def tool_results_turn(results):
        return AnthropicProvider.tool_results_turn(results)


# --------------------------------------------------------------------------
# Null provider
# --------------------------------------------------------------------------

class NoProvider(Provider):
    """
    No model configured.

    Returns an explicit unavailability rather than raising. Callers fall back
    to deterministic paths and say so in their output, which keeps a run
    without an API key both possible and honest.
    """
    name = "none"
    available = False
    reason = "no provider configured"

    def complete(self, system, messages, tools=None, max_tokens=1024):
        return LLMResponse(provider=self.name,
                           error="no language model provider configured")

    @staticmethod
    def assistant_turn(resp):
        return {"role": "assistant", "content": []}

    @staticmethod
    def tool_results_turn(results):
        return AnthropicProvider.tool_results_turn(results)


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

REGISTRY = {
    "anthropic": AnthropicProvider,
    "groq": GroqProvider,
    "gemini": GeminiProvider,
    "none": NoProvider,
}

PREFERENCE = ["anthropic", "groq", "gemini"]


def get_provider(name: str | None = None) -> Provider:
    """
    Resolve a provider.

    An explicitly named provider is returned even if unavailable, so a
    misconfiguration surfaces as a clear message rather than silently falling
    through to a different vendor and producing results the caller did not
    expect.
    """
    requested = name or os.environ.get("RECON_LLM_PROVIDER")
    if requested:
        cls = REGISTRY.get(requested.lower())
        if cls is None:
            raise ValueError(f"unknown provider '{requested}'; "
                             f"expected one of {sorted(REGISTRY)}")
        return cls()

    for candidate in PREFERENCE:
        p = REGISTRY[candidate]()
        if p.available:
            return p
    return NoProvider()


def describe() -> str:
    lines = ["language model providers"]
    for key in PREFERENCE:
        p = REGISTRY[key]()
        mark = "available" if p.available else f"unavailable ({p.reason})"
        model = getattr(p, "model", "-")
        lines.append(f"  {key:<10} {mark:<52} {model}")
    active = get_provider()
    lines.append("")
    lines.append(f"  active: {active.name}")
    if not active.available:
        lines.append("  deterministic paths only; model-dependent steps "
                     "will report themselves as unavailable")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
