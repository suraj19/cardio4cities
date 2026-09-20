"""
Thin wrapper around any OpenAI-compatible chat-completions endpoint —
Mistral (the default), Gemini, DeepSeek, Groq, OpenRouter, OpenAI or a
local Ollama, selected by LLM_BASE_URL and the model settings alone.

"Compatible" is not uniform: a shim will typically reject a parameter it
does not implement rather than ignore it, so optional fields are only
included when configured. See the reasoning_effort handling below.

Calls are made against one of two profiles rather than one model, because
the pipeline makes two kinds of request and they do not want the same thing:

  JUDGEMENT  ~30 calls per run. Planning queries, adjudicating a claim,
             writing the narrative, answering /ask. A wrong answer here is
             visible in the brief, so these get the better model and enough
             thinking budget to deliberate.
  BULK       ~120 calls per run. Turning a page body into claim JSON, and
             Graphiti's entity extraction underneath the graph writes. These
             fill a fixed schema from text that is in front of them; there is
             no judgement to make and nothing to think about, so they get the
             cheap model and no thinking at all.

That split is the whole cost story on a thinking model, where thinking tokens
bill at the output rate: the bulk path is 80% of the calls and, on the
defaults, a third of the price per token. Routing it to the judgement model
is not a quality upgrade, it is a 3x bill for identical JSON.

In MOCK mode, returns deterministic canned responses so the graph can be
exercised with zero API keys/internet.
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass

from app.config import settings
from app.llm.usage import ledger
from app.telemetry import content_attributes, record_llm_usage, span

_MAX_ATTEMPTS = 4
_BACKOFF_SECONDS = 2.0

# Matched on the message rather than on exception classes, so this works
# across providers and across openai-python versions without importing
# vendor-specific error types.
_RETRYABLE_MARKERS = (
    "rate limit",
    "rate_limit",
    "429",
    "resource_exhausted",
    "quota",
    "overloaded",
    "unavailable",
    "503",
    "504",
    "timeout",
    "timed out",
    "connection",
)


def _is_retryable(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)


def _system_name() -> str:
    """The provider, for `gen_ai.system`, derived from the endpoint.

    Derived rather than configured because the endpoint is already the single
    thing that selects a provider here, and a second setting that could
    disagree with it would eventually disagree with it.
    """
    host = settings.LLM_BASE_URL.lower()
    for marker, name in (
        ("mistral", "mistral"),
        ("openai.com", "openai"),
        ("googleapis", "gcp.gemini"),
        ("deepseek", "deepseek"),
        ("groq", "groq"),
        ("openrouter", "openrouter"),
        ("11434", "ollama"),
        ("localhost", "local"),
        ("127.0.0.1", "local"),
    ):
        if marker in host:
            return name
    return "openai_compatible"


def _answer_text(message) -> str:
    """The assistant's answer as a string, with any reasoning trace dropped.

    `message.content` is not always a string. A model asked to reason out
    loud can return a list of typed chunks instead — Mistral Small 4 at
    reasoning_effort="high" returns a "thinking" chunk followed by a "text"
    chunk. Every caller here feeds this straight to json.loads or into the
    brief, so the list form does not raise: it stringifies into something
    that parses as nothing, and the run reports a page with no claims.

    Returning only the text chunks means the reasoning is spent improving the
    answer and then discarded, which is what we want at every call site —
    none of them is multi-turn, so there is no trace to replay.
    """
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for chunk in content:
            # Mistral's SDK returns objects; a raw HTTP shim returns dicts.
            kind = getattr(chunk, "type", None) or (
                chunk.get("type") if isinstance(chunk, dict) else None
            )
            if kind and kind != "text":
                continue
            text = getattr(chunk, "text", None) or (
                chunk.get("text") if isinstance(chunk, dict) else None
            )
            if text:
                parts.append(text)
        return "".join(parts)
    return ""


@dataclass(frozen=True)
class Profile:
    """A model together with the two settings that have to move with it.

    Grouped rather than passed separately because they are not independent:
    a reasoning effort is only meaningful against a model that implements it,
    and an output ceiling has to leave room for whatever that effort spends.
    Splitting them across call sites is how a model change turns into a run
    where every response is silently empty.
    """

    model: str
    reasoning_effort: str
    max_tokens: int


def judgement_profile() -> Profile:
    return Profile(
        model=settings.LLM_MODEL,
        reasoning_effort=settings.LLM_REASONING_EFFORT,
        max_tokens=settings.LLM_MAX_TOKENS,
    )


def bulk_profile() -> Profile:
    return Profile(
        model=settings.LLM_BULK_MODEL,
        reasoning_effort=settings.LLM_BULK_REASONING_EFFORT,
        max_tokens=settings.LLM_BULK_MAX_TOKENS,
    )


# Read through functions rather than bound at import, so a test that patches
# `settings` sees the change. The agents call these; nothing else should need
# to know which model it is talking to.
def _profile(bulk: bool) -> Profile:
    return bulk_profile() if bulk else judgement_profile()


class LLMClient:
    def __init__(self):
        self._client = None

    def _get_client(self):
        """Built on first use rather than at import, so a missing key surfaces
        as a failed request instead of preventing the app from starting (which
        would take /health down with it and leave the container restarting)."""
        if self._client is None:
            if not settings.LLM_API_KEY and not settings.llm_is_local:
                raise ValueError(
                    "LLM_API_KEY is required when RUN_MODE=LIVE. Set it in .env "
                    "alongside LLM_BASE_URL and LLM_MODEL, or switch to RUN_MODE=MOCK. "
                    "To run inference locally instead, point LLM_BASE_URL at "
                    "http://localhost:11434/v1 (Ollama) and no key is needed."
                )

            from openai import OpenAI

            self._client = OpenAI(
                # The SDK refuses to construct without a key, and a local
                # server ignores whatever it is sent, so the placeholder is
                # what makes a keyless local run possible at all. Only used
                # when LLM_API_KEY is genuinely absent — pointing at Ollama
                # does not discard a key that was set.
                api_key=settings.LLM_API_KEY or "local",
                base_url=settings.LLM_BASE_URL,
            )
        return self._client

    def complete(
        self,
        system: str,
        user: str,
        *,
        bulk: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        """One chat completion.

        `bulk=True` selects the cheap, minimally-thinking profile and is
        correct for anything that fills a fixed JSON schema from text it has
        been handed. Everything else gets the judgement profile — see the
        module docstring for why that distinction is the entire cost story.
        """
        profile = _profile(bulk)

        if settings.is_mock:
            # Traced too, so the shape of a MOCK trace matches a LIVE one.
            # That is what makes the instrumentation testable offline — and a
            # MOCK run whose trace is missing a span is how you find a call
            # site nobody instrumented. `gen_ai.system` says "mock" so no
            # reader mistakes these for calls that cost anything.
            with span(
                f"chat {profile.model}",
                **{
                    "gen_ai.operation.name": "chat",
                    "gen_ai.system": "mock",
                    "gen_ai.request.model": profile.model,
                    "cardio4cities.llm.profile": "bulk" if bulk else "judgement",
                    **content_attributes(system=system, user=user),
                },
            ) as current:
                answer = self._mock_complete(system, user)
                current.set_attribute("cardio4cities.llm.answer_chars", len(answer))
                current.set_attributes(content_attributes(completion=answer))
                return answer

        kwargs = {}
        if profile.reasoning_effort:
            # Caps thinking on models that implement it — "none" on Mistral
            # Small 4, "minimal"/"low" on Gemini 3 and the o-series. Omitted
            # entirely when blank, because a shim that does not implement it
            # rejects the request rather than ignoring the field, and Mistral
            # Large 3 is one such model.
            kwargs["reasoning_effort"] = profile.reasoning_effort

        request = dict(
            model=profile.model,
            max_tokens=max_tokens or profile.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
        prompt_chars = len(system) + len(user)
        # Attribute names follow the OpenTelemetry GenAI semantic conventions
        # so a backend recognises this as a model call rather than an
        # anonymous span. Prompts and completions are recorded only when
        # OTEL_CAPTURE_CONTENT is explicitly on — they carry scraped page
        # content, so the default is not to copy it into a trace backend.
        span_attributes = {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": _system_name(),
            "gen_ai.request.model": profile.model,
            "gen_ai.request.max_tokens": request["max_tokens"],
            "cardio4cities.llm.profile": "bulk" if bulk else "judgement",
            "cardio4cities.llm.prompt_chars": prompt_chars,
        }
        if profile.reasoning_effort:
            span_attributes["gen_ai.request.reasoning_effort"] = profile.reasoning_effort
        span_attributes.update(content_attributes(system=system, user=user))

        # Extraction and fact-checking fan out to LLM_MAX_CONCURRENCY parallel
        # calls, which on a free-tier key reliably earns a 429. Retrying here
        # rather than letting the caller's fallback fire matters: an exhausted
        # fact-check call fails safe to UNSUPPORTED, so a rate limit would
        # silently read as "this claim could not be corroborated".
        last_error: Exception | None = None
        # One span covers the whole call INCLUDING its retries, with each
        # attempt as an event. A span per attempt would be more literal and
        # much less useful: what a reader wants to know is how long this call
        # took end to end, and a rate-limited call that succeeded on its
        # fourth attempt after 30 seconds of backoff is the single most
        # important thing this span can show.
        with span(f"chat {profile.model}", **span_attributes) as current:
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    response = self._get_client().chat.completions.create(**request)
                except Exception as exc:
                    last_error = exc
                    retrying = _is_retryable(exc) and attempt < _MAX_ATTEMPTS - 1
                    # Each attempt is recorded, including the ones that will be
                    # retried. A run whose cost looks inexplicable is usually a
                    # run that spent it on retries, and a ledger that counted
                    # only successes could not show that.
                    ledger.record(
                        profile.model, prompt_chars=prompt_chars, failed=True
                    )
                    record_llm_usage(profile.model, 0, 0, failed=True)
                    current.add_event(
                        "llm.attempt.failed",
                        {
                            "attempt": attempt + 1,
                            "retrying": retrying,
                            "error.type": type(exc).__name__,
                            "error.message": str(exc)[:300],
                        },
                    )
                    if not retrying:
                        raise
                    time.sleep(_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 0.5))
                    continue

                usage = getattr(response, "usage", None)
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                ledger.record(
                    profile.model,
                    prompt_chars=prompt_chars,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
                record_llm_usage(profile.model, prompt_tokens, completion_tokens)
                current.set_attributes(
                    {
                        "gen_ai.usage.input_tokens": prompt_tokens,
                        "gen_ai.usage.output_tokens": completion_tokens,
                        "cardio4cities.llm.attempts": attempt + 1,
                    }
                )
                # A thinking model that exhausts its budget mid-thought returns
                # no content. Every caller parses this, so hand back a string
                # rather than None and let their fallbacks handle it.
                answer = _answer_text(response.choices[0].message)
                # Length, not content. An empty answer is the signature of a
                # blown thinking budget, and it is otherwise invisible: the
                # call succeeded, the tokens were spent, and the caller
                # silently falls back.
                current.set_attribute("cardio4cities.llm.answer_chars", len(answer))
                current.set_attributes(content_attributes(completion=answer))
                return answer

        raise last_error  # unreachable; keeps the type checker honest

    # -----------------------------------------------------------------
    # MOCK behavior — deliberately simple/deterministic, not "smart".
    # The point is to exercise the GRAPH LOGIC, not to simulate a good LLM.
    # -----------------------------------------------------------------
    def _mock_complete(self, system: str, user: str) -> str:
        system_lower = system.lower()

        if "generate web search queries" in system_lower:
            city = self._echo(user, "City") or "the city"
            # Mirrors the real contract: an object keyed by dimension. The
            # dimensions asked for are echoed back from the prompt, so this
            # stays correct if the dimension set changes.
            dimensions = re.findall(r"^- ([a-z_]+):", user, flags=re.MULTILINE)
            return json.dumps(
                {
                    dimension: [f"{city} {dimension.replace('_', ' ')} official source"]
                    for dimension in dimensions
                }
            )

        if "extract atomic factual claims" in system_lower:
            dimension = self._echo(user, "Dimension of interest") or "healthcare programmes"
            topic = dimension.split(":")[0].strip().rstrip(".")
            return json.dumps(
                [
                    {
                        "text": f"The city health department runs a subsidized blood "
                        f"pressure screening programme at primary health centres "
                        f"({topic[:60].lower()}).",
                        "is_city_level": True,
                    }
                ]
            )

        if "independent fact-checker" in system_lower:
            # Deliberately SINGLE_SOURCE with no supporting sources named, so
            # the tier-derivation rule in fact_check_agent is exercised rather
            # than bypassed by a mock that hands back VERIFIED for free.
            return json.dumps(
                {
                    "tier": "SINGLE_SOURCE",
                    "supporting_sources": [],
                    "conflicting_text": None,
                    "reasoning": "Only one source mentions this specific programme; no "
                    "corroborating source was found among the labelled passages.",
                    "national_vs_city_flag": False,
                }
            )

        if "city intelligence assistant" in system_lower:
            return (
                "MOCK-mode answer. In LIVE mode this is generated strictly from the "
                "numbered evidence below the question, with inline citations such as "
                "[1] pointing at the source URL or graph edge it came from [1]."
            )

        if "briefing summary" in system_lower:
            return (
                "This is a MOCK-mode narrative summary. In LIVE mode this paragraph "
                "is generated by the configured LLM from the fact-checked statements "
                "listed below, and introduces no information not already present in "
                "them. Gaps are reported rather than filled."
            )

        return "[MOCK RESPONSE — no matching mock handler for this prompt]"

    @staticmethod
    def _echo(user: str, field: str) -> str:
        """Pull a labelled value back out of the user prompt so mock output
        varies with its input instead of being a single frozen string."""
        match = re.search(rf"^{re.escape(field)}:\s*(.+)$", user, flags=re.MULTILINE)
        return match.group(1).strip() if match else ""


llm_client = LLMClient()
