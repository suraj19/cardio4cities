"""
Small helpers shared across layers.

`domain_of` lives here because three separate modules need the same answer
to the same question — the crawlability gate compares a URL's domain against
the ToS denylist, the extractor stamps a passage with it, and the relational
store indexes sources by it. They previously each had their own copy, which
is exactly the kind of duplication that drifts: if one of them stops
stripping `www.` the fact-checker quietly starts treating `www.example.org`
and `example.org` as two independent sources.

`relevant_window` is here for the same reason. Both LLM call sites that send
page text now choose *which* part of it to send rather than taking the top —
the extractor against the dimension brief, the fact-checker against the claim
— and that selection is the mechanism the whole input-token budget rests on.
One copy, so tightening the budget tightens both.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

_WORD = re.compile(r"[a-z0-9]{4,}")


def domain_of(url: str) -> str:
    """Registrable host for a URL, lowercased and without a `www.` prefix.

    The `www.` strip is load-bearing rather than cosmetic: the fact-checker
    counts distinct domains to decide whether a claim is independently
    corroborated, so two spellings of one host must normalise to one domain.
    """
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def domain_matches(domain: str, patterns) -> bool:
    """Whether `domain` is, or sits under, any of `patterns`.

    Suffix matching on a dot boundary, so `gov.in` covers
    `nhm.maharashtra.gov.in` while `notgov.in` matches neither. The dot
    matters: a plain `endswith` would let `evilgov.in` pass as `gov.in`,
    which for an allowlist is the difference between a policy and a
    suggestion.
    """
    domain = (domain or "").lower().strip().lstrip(".")
    if not domain:
        return False
    return any(
        domain == pattern or domain.endswith(f".{pattern}")
        for pattern in patterns
    )


def tokens(text: str) -> set[str]:
    """Words of four characters or more, lowercased. Long enough to skip the
    stopwords that would otherwise dominate an overlap score."""
    return set(_WORD.findall(text.lower()))


def relevant_window(text: str, wanted: set[str], budget: int) -> str:
    """The `budget` characters of `text` with the most overlap with `wanted`.

    Head truncation is the cheapest possible choice and usually the wrong
    one: on a long programme page or an annual report, the sentence carrying
    the figure that settles a question is rarely in the opening paragraph,
    which is a masthead and a cookie notice. The cost is identical — the same
    number of characters is sent either way — so taking the top was paying
    full price for boilerplate.

    That is what makes a smaller budget viable rather than merely cheaper:
    2500 relevant characters carry more signal than 5000 characters that
    start at the top of the page and stop halfway down.
    """
    if len(text) <= budget:
        return text
    step = max(budget // 4, 1)
    last_start = len(text) - budget

    # The final start is evaluated explicitly because striding does not
    # generally land on it: with an 8107-character page and a 2500 budget the
    # stride stops at 5000, so nothing past 7500 is ever scored. That made the
    # tail of a long page unreachable while still paying for 2500 characters
    # of it — the same failure head truncation makes, moved to the other end,
    # and the end of a report is where the summary tables live.
    starts = [*range(0, last_start, step), last_start]

    best_start, best_score = 0, -1
    for start in starts:
        score = len(wanted & tokens(text[start : start + budget]))
        if score > best_score:
            best_start, best_score = start, score
    return text[best_start : best_start + budget]
