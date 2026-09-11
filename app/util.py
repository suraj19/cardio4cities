"""
Small helpers shared across layers.

`domain_of` lives here because three separate modules need the same answer
to the same question — the crawlability gate compares a URL's domain against
the ToS denylist, the extractor stamps a passage with it, and the relational
store indexes sources by it. They previously each had their own copy, which
is exactly the kind of duplication that drifts: if one of them stops
stripping `www.` the fact-checker quietly starts treating `www.example.org`
and `example.org` as two independent sources.
"""
from __future__ import annotations

from urllib.parse import urlparse


def domain_of(url: str) -> str:
    """Registrable host for a URL, lowercased and without a `www.` prefix.

    The `www.` strip is load-bearing rather than cosmetic: the fact-checker
    counts distinct domains to decide whether a claim is independently
    corroborated, so two spellings of one host must normalise to one domain.
    """
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc
