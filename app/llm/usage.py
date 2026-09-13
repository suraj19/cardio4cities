"""
Token and call accounting for the LLM client.

This exists because "reduce the token cost" is otherwise an unfalsifiable
claim. The pipeline's spend is spread across four call sites with very
different shapes — one extraction call per source, one adjudication call per
claim group, one narrative, and Graphiti's own extraction underneath all of
it — and the only number anyone ever saw was the provider's monthly bill,
which arrives too late to attribute to anything.

So every call records what it cost, keyed by model. A run reports its own
delta (see app/jobs.py), which turns a tuning change into a before/after
measurement rather than an argument.

Two deliberate limitations:

  * `prompt_chars` is counted for every call; token counts only appear when
    the provider returns a `usage` block. Most OpenAI-compatible shims do,
    but not all, and a shim that omits it would otherwise make the whole
    ledger read as zero. Characters are always available and roughly
    proportional, so they are the fallback signal.
  * The ledger is process-wide, and a delta is taken across a job's
    lifetime. Two research runs overlapping will therefore each report the
    other's usage as well. Attributing exactly would mean threading a
    collector through the thread pools that extraction and fact-checking run
    on, and the number is used for tuning, not billing.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class ModelUsage:
    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_chars: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class UsageSnapshot:
    """Per-model usage at a point in time, and the arithmetic to diff two."""

    by_model: dict[str, ModelUsage] = field(default_factory=dict)

    def __sub__(self, earlier: "UsageSnapshot") -> "UsageSnapshot":
        delta: dict[str, ModelUsage] = {}
        for model, now in self.by_model.items():
            before = earlier.by_model.get(model, ModelUsage())
            entry = ModelUsage(
                calls=now.calls - before.calls,
                failures=now.failures - before.failures,
                prompt_tokens=now.prompt_tokens - before.prompt_tokens,
                completion_tokens=now.completion_tokens - before.completion_tokens,
                prompt_chars=now.prompt_chars - before.prompt_chars,
            )
            if entry.calls or entry.failures or entry.prompt_chars:
                delta[model] = entry
        return UsageSnapshot(by_model=delta)

    def as_dict(self) -> dict:
        """The shape the API returns. Totals are included because the
        per-model breakdown is the interesting part only once someone is
        already looking at the number."""
        models = {
            model: {
                "calls": usage.calls,
                "failures": usage.failures,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "prompt_chars": usage.prompt_chars,
            }
            for model, usage in sorted(self.by_model.items())
        }
        return {
            "calls": sum(u.calls for u in self.by_model.values()),
            "failures": sum(u.failures for u in self.by_model.values()),
            "prompt_tokens": sum(u.prompt_tokens for u in self.by_model.values()),
            "completion_tokens": sum(
                u.completion_tokens for u in self.by_model.values()
            ),
            "total_tokens": sum(u.total_tokens for u in self.by_model.values()),
            "by_model": models,
        }


class UsageLedger:
    """Running totals, safe to update from the extraction and fact-check
    thread pools as well as from the event loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_model: dict[str, ModelUsage] = {}

    def record(
        self,
        model: str,
        *,
        prompt_chars: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        failed: bool = False,
    ) -> None:
        with self._lock:
            current = self._by_model.get(model, ModelUsage())
            self._by_model[model] = replace(
                current,
                calls=current.calls + 1,
                failures=current.failures + (1 if failed else 0),
                prompt_tokens=current.prompt_tokens + prompt_tokens,
                completion_tokens=current.completion_tokens + completion_tokens,
                prompt_chars=current.prompt_chars + prompt_chars,
            )

    def snapshot(self) -> UsageSnapshot:
        with self._lock:
            return UsageSnapshot(by_model=dict(self._by_model))

    def reset(self) -> None:
        """Only for tests — the ledger is cumulative for the process life."""
        with self._lock:
            self._by_model.clear()


ledger = UsageLedger()
