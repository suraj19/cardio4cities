"""
Thin wrapper around any OpenAI-compatible chat-completions endpoint —
Mistral (the default), Gemini, DeepSeek, Groq, OpenRouter, OpenAI or a
local Ollama, selected by LLM_BASE_URL and LLM_MODEL alone.

"Compatible" is not uniform: a shim will typically reject a parameter it
does not implement rather than ignore it, so optional fields are only
included when configured. See the reasoning_effort handling below.

In MOCK mode, returns deterministic canned responses so the graph can be
exercised with zero API keys/internet.
"""
import json
import random
import re
import time

from app.config import settings

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


class LLMClient:
    def __init__(self):
        self._client = None

    def _get_client(self):
        """Built on first use rather than at import, so a missing key surfaces
        as a failed request instead of preventing the app from starting (which
        would take /health down with it and leave the container restarting)."""
        if self._client is None:
            if not settings.LLM_API_KEY:
                raise ValueError(
                    "LLM_API_KEY is required when RUN_MODE=LIVE. Set it in .env "
                    "alongside LLM_BASE_URL and LLM_MODEL, or switch to RUN_MODE=MOCK."
                )

            from openai import OpenAI

            self._client = OpenAI(
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
            )
        return self._client

    def complete(self, system: str, user: str, max_tokens: int | None = None) -> str:
        if settings.is_mock:
            return self._mock_complete(system, user)

        kwargs = {}
        if settings.LLM_REASONING_EFFORT:
            # Caps thinking on models that reason by default (Gemini 3, o-series).
            kwargs["reasoning_effort"] = settings.LLM_REASONING_EFFORT

        request = dict(
            model=settings.LLM_MODEL,
            max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )

        # Extraction and fact-checking fan out to LLM_MAX_CONCURRENCY parallel
        # calls, which on a free-tier key reliably earns a 429. Retrying here
        # rather than letting the caller's fallback fire matters: an exhausted
        # fact-check call fails safe to UNSUPPORTED, so a rate limit would
        # silently read as "this claim could not be corroborated".
        last_error: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._get_client().chat.completions.create(**request)
                # A thinking model that exhausts its budget mid-thought returns
                # no content. Every caller parses this, so hand back a string
                # rather than None and let their fallbacks handle it.
                return response.choices[0].message.content or ""
            except Exception as exc:
                last_error = exc
                if not _is_retryable(exc) or attempt == _MAX_ATTEMPTS - 1:
                    raise
                time.sleep(_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 0.5))

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
