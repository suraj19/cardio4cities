"""
Tolerant JSON parsing for LLM output.

Every agent here asks the model for "ONLY JSON" and most models comply
*semantically* while still wrapping the payload in a ```json fence, or
prefixing it with "Here is the JSON:". A bare json.loads() treats all of
that as a hard failure, which in this pipeline is expensive: the
extraction agent silently produces zero claims, and the fact-checker
fail-safes every claim to UNSUPPORTED. The result looks like "the city
has no public health data" when it actually means "the model used a code
fence".

So parsing is deliberately forgiving about packaging and strict about
content: we hunt for the first balanced JSON value in the string, and if
there genuinely isn't one, the caller's typed default is returned.
"""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE_OPEN = re.compile(r"```[a-zA-Z0-9_-]*\s*")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if "```" in text:
        text = _FENCE_OPEN.sub("", text).replace("```", "")
    return text.strip()


def _first_balanced_value(text: str) -> str | None:
    """Return the first complete JSON array or object in `text`.

    Scans with a depth counter that is string- and escape-aware, so a
    bracket inside a quoted value (common in URLs and prose) doesn't end
    the span early.
    """
    start = next((i for i, ch in enumerate(text) if ch in "[{"), None)
    if start is None:
        return None

    opener = text[start]
    closer = "]" if opener == "[" else "}"
    depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_json(raw: str, default: Any) -> Any:
    """Best-effort parse of `raw`, falling back to `default`."""
    if not raw:
        return default

    cleaned = _strip_fences(raw)
    for candidate in (cleaned, _first_balanced_value(cleaned)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return default


def parse_json_list(raw: str) -> list:
    """Parse into a list. A single object is wrapped, because models asked
    for a list of one item frequently return the bare object."""
    value = _parse_json(raw, default=[])
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def parse_json_object(raw: str) -> dict:
    """Parse into a dict, or an empty dict. Callers treat empty as failure
    and apply their own conservative fallback."""
    value = _parse_json(raw, default={})
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}
