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
    models: list[str] = []
    default_model = ""

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
    models = ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]

    def __init__(self, model: str | None = None):
        override = model or os.environ.get("RECON_ANTHROPIC_MODEL")
        self.candidates = [override] if override else list(self.models)
        self.model = self.candidates[0]
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

    def complete(self, system, messages, tools=None, max_tokens=1024,
                 model: str | None = None):
        try:
            kwargs: dict[str, Any] = {
                "model": model or self.model,
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

class OpenAICompatibleProvider(Provider):
    """
    Base for any vendor exposing an OpenAI-compatible chat completions API.

    Five providers below differ only in base URL, environment variable and
    default model. Expressing them as configuration of one adapter rather than
    five classes means the awkward part -- translating Anthropic's tool-result
    blocks into OpenAI's separate "tool" messages -- is written and debugged
    once.
    """
    name = "openai-compatible"
    base_url = ""
    env_key = ""
    models: list[str] = []
    install_hint = "pip install openai"

    def __init__(self, model: str | None = None):
        override = model or os.environ.get(f"RECON_{self.name.upper()}_MODEL")
        self.candidates = [override] if override else list(self.models)
        self.model = self.candidates[0] if self.candidates else ""
        self.available = False
        self._client = None
        key = os.environ.get(self.env_key)
        if not key:
            self.reason = f"{self.env_key} not set"
            return
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=key, base_url=self.base_url)
            self.available = True
            self.reason = ""
        except ImportError:
            self.reason = f"openai SDK not installed ({self.install_hint})"

    @staticmethod
    def _translate_tools(tools: list[dict]) -> list[dict]:
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
        OpenAI wants one message per result with role "tool". An assistant turn
        that called tools must also carry a tool_calls array, or the follow-up
        tool messages are rejected as orphaned.
        """
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            content = m["content"]
            if isinstance(content, str):
                out.append({"role": m["role"], "content": content})
                continue

            if m["role"] == "assistant":
                text = "".join(b.get("text", "") for b in content
                               if isinstance(b, dict) and b.get("type") == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"],
                                       "arguments": json.dumps(b["input"])}}
                         for b in content
                         if isinstance(b, dict) and b.get("type") == "tool_use"]
                msg: dict[str, Any] = {"role": "assistant",
                                       "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    out.append({"role": "tool",
                                "tool_call_id": block["tool_use_id"],
                                "content": block["content"]})
                elif block.get("type") == "text":
                    out.append({"role": "user", "content": block["text"]})
        return out

    def complete(self, system, messages, tools=None, max_tokens=1024,
                 model: str | None = None):
        try:
            kwargs: dict[str, Any] = {
                "model": model or self.model,
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
        # Re-expressed in the internal Anthropic shape so the agent loop never
        # needs to know which vendor produced the turn.
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


class GroqProvider(OpenAICompatibleProvider):
    name = "groq"
    base_url = "https://api.groq.com/openai/v1"
    env_key = "GROQ_API_KEY"
    models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b",
              "qwen/qwen3.6-27b", "llama-3.3-70b-versatile"]
    # Groq retires models on a rolling basis; llama-3.3-70b-versatile was
    # deprecated in June 2026. Override with RECON_GROQ_MODEL if this one
    # has been retired too -- the failure mode is a 404 naming the model,
    # which the failover chain reports rather than swallowing.
    default_model = "openai/gpt-oss-120b"


class CerebrasProvider(OpenAICompatibleProvider):
    name = "cerebras"
    base_url = "https://api.cerebras.ai/v1"
    env_key = "CEREBRAS_API_KEY"
    models = ["gpt-oss-120b", "llama-3.3-70b"]
    models = ["gpt-oss-120b", "llama-3.3-70b", "llama3.1-8b"]


class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1"
    env_key = "OPENROUTER_API_KEY"
    models = ["meta-llama/llama-3.3-70b-instruct", "google/gemini-2.0-flash-001", "mistralai/mistral-large"]
    models = ["meta-llama/llama-3.3-70b-instruct",
              "openai/gpt-oss-120b",
              "google/gemini-2.0-flash-001",
              "mistralai/mistral-large"]


class TogetherProvider(OpenAICompatibleProvider):
    name = "together"
    base_url = "https://api.together.xyz/v1"
    env_key = "TOGETHER_API_KEY"
    models = ["meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-72B-Instruct-Turbo"]
    models = ["meta-llama/Llama-3.3-70B-Instruct-Turbo",
              "Qwen/Qwen2.5-72B-Instruct-Turbo"]


class MistralProvider(OpenAICompatibleProvider):
    name = "mistral"
    base_url = "https://api.mistral.ai/v1"
    env_key = "MISTRAL_API_KEY"
    models = ["mistral-large-latest", "mistral-small-latest"]
    models = ["mistral-large-latest", "mistral-small-latest"]


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------

class GeminiProvider(Provider):
    name = "gemini"
    models = ["gemini-2.0-flash", "gemini-2.0-flash-lite",
              "gemini-1.5-flash"]

    def __init__(self, model: str | None = None):
        override = model or os.environ.get("RECON_GEMINI_MODEL")
        self.candidates = [override] if override else list(self.models)
        self.model = self.candidates[0]
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

    def complete(self, system, messages, tools=None, max_tokens=1024,
                 model: str | None = None):
        try:
            from google.genai import types
            cfg: dict[str, Any] = {"max_output_tokens": max_tokens}
            if tools:
                cfg["tools"] = [types.Tool(function_declarations=[
                    types.FunctionDeclaration(
                        name=t["name"], description=t["description"],
                        parameters=t["input_schema"]) for t in tools])]
            resp = self._client.models.generate_content(
                model=model or self.model,
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

    def complete(self, system, messages, tools=None, max_tokens=1024,
                 model: str | None = None):
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

REGISTRY: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "groq": GroqProvider,
    "cerebras": CerebrasProvider,
    "gemini": GeminiProvider,
    "openrouter": OpenRouterProvider,
    "together": TogetherProvider,
    "mistral": MistralProvider,
    "none": NoProvider,
}

# Order matters. Anthropic first because the agent's tool-use loop was designed
# against it; then the free tiers most likely to be configured; then the paid
# aggregators. A vendor absent from the environment is skipped silently, so the
# chain is however many providers the operator actually has keys for.
PREFERENCE = ["anthropic", "groq", "cerebras", "gemini",
              "openrouter", "together", "mistral"]


# Distinguishing failure kinds matters. A retired model identifier should cost
# that one model; an unfunded account or a revoked key should cost the whole
# provider. Treating them alike -- as the first version of this chain did --
# discards a working provider because one of its model names went stale.

MODEL_LEVEL_MARKERS = ("does not exist", "model_not_found", "unknown model",
                       "invalid model", "decommissioned", "not found",
                       "404", "no access to")

PROVIDER_LEVEL_MARKERS = ("credit balance", "insufficient", "quota",
                          "authentication", "invalid api key", "unauthorized",
                          "401", "403", "billing", "payment")


def classify_failure(error: str) -> str:
    """Return 'model' or 'provider' for a failure message."""
    low = error.lower()
    if any(m in low for m in PROVIDER_LEVEL_MARKERS):
        return "provider"
    if any(m in low for m in MODEL_LEVEL_MARKERS):
        return "model"
    # Unknown failures are treated as model-level, which is the conservative
    # choice: it costs one candidate rather than discarding a provider that
    # may work with a different model.
    return "model"


class FallbackChain(Provider):
    """
    Walks provider/model candidates in order, moving on when one fails.

    Selecting once at startup is not enough. A key can be valid with an empty
    account; a free tier can rate-limit mid-run; a vendor can retire a model
    identifier between releases. Any of those leaves a single-candidate setup
    dead in the middle of a run.

    Failover is per call. A failed candidate is demoted for the rest of the
    session rather than retried on every subsequent call -- retrying a dead
    candidate once per record wastes the run and buries the real error under
    repeats.

    Demotion is scoped by failure kind: a retired model costs that model, a
    dead account costs the provider. Every demotion is recorded in `events`
    so a run reports which candidates were used and why it moved, rather than
    silently answering from a model the operator did not expect.
    """
    name = "chain"

    def __init__(self, providers: list[Provider]):
        self.providers = [p for p in providers if p.available]
        self.available = bool(self.providers)
        self.reason = "" if self.available else "no provider has a usable key"
        self.dead_models: set[tuple[str, str]] = set()
        self.dead_providers: set[str] = set()
        self.events: list[str] = []
        self.active: Provider | None = (self.providers[0]
                                        if self.providers else None)
        self.active_model = (self.providers[0].candidates[0]
                             if self.providers else "")

    @property
    def candidates(self) -> list[tuple[str, str]]:
        return [(p.name, m) for p in self.providers for m in p.candidates]

    @property
    def chain_names(self) -> list[str]:
        return [f"{p.name}:{p.candidates[0]}" for p in self.providers]

    def complete(self, system, messages, tools=None, max_tokens=1024,
                 model: str | None = None):
        last = None
        for p in self.providers:
            if p.name in self.dead_providers:
                continue
            for m in p.candidates:
                if (p.name, m) in self.dead_models:
                    continue

                resp = p.complete(system, messages, tools, max_tokens, model=m)
                if resp.ok:
                    self.active, self.active_model = p, m
                    return resp

                last = resp
                kind = classify_failure(resp.error)
                if kind == "provider":
                    self.dead_providers.add(p.name)
                    self.events.append(
                        f"{p.name} unusable ({resp.error[:70]}); "
                        f"provider demoted")
                    break
                self.dead_models.add((p.name, m))
                self.events.append(
                    f"{p.name}:{m} unusable ({resp.error[:70]}); "
                    f"model demoted, other models on this provider still live")

        return last or LLMResponse(
            provider=self.name,
            error="every configured provider and model failed")

    def assistant_turn(self, resp):
        for p in self.providers:
            if p.name == resp.provider:
                return p.assistant_turn(resp)
        return AnthropicProvider.assistant_turn(resp)

    def tool_results_turn(self, results):
        return AnthropicProvider.tool_results_turn(results)


def get_provider(name: str | None = None) -> Provider:
    """
    Resolve a provider.

    An explicitly named provider is returned even if unavailable, so a
    misconfiguration surfaces as a clear message rather than silently falling
    through to a different vendor and producing results the caller did not
    expect. With no name given, every configured provider is chained.
    """
    requested = name or os.environ.get("RECON_LLM_PROVIDER")
    if requested:
        cls = REGISTRY.get(requested.lower())
        if cls is None:
            raise ValueError(f"unknown provider '{requested}'; "
                             f"expected one of {sorted(REGISTRY)}")
        return cls()

    chain = FallbackChain([REGISTRY[k]() for k in PREFERENCE])
    return chain if chain.available else NoProvider()


def describe() -> str:
    lines = ["language model providers", ""]
    for key in PREFERENCE:
        p = REGISTRY[key]()
        mark = "available" if p.available else f"unavailable ({p.reason})"
        lines.append(f"  {key:<12} {mark:<50} {getattr(p, 'model', '-')}")

    active = get_provider()
    lines.append("")
    if isinstance(active, FallbackChain):
        pairs = active.candidates
        lines.append(f"  failover chain: {len(pairs)} provider/model "
                     f"candidate(s)")
        for name, m in pairs:
            lines.append(f"    {name}:{m}")
        lines.append("")
        lines.append("  a retired model demotes that model; a dead account "
                     "demotes the provider")
    else:
        lines.append(f"  active: {active.name}")
        lines.append("  deterministic paths only; model-dependent steps will "
                     "report themselves as unavailable")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
