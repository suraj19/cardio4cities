"""
Extraction Agent.

Fetches full content ONLY for URLs whose CrawlabilityResult was ALLOWED.
That is enforced structurally: `allowed_urls` is built from the crawl
verdicts and nothing outside it is ever requested, so the gate cannot be
bypassed by a bug elsewhere in the graph.

Then extracts atomic factual claims from each passage using the LLM,
told which dimension the source was found for and which city is the
subject — the latter is what lets it set is_city_level=false when a page
about the country is being read for signal about the city.

Fetch and extraction run together on a thread pool, one task per URL.
They are independent and both dominated by waiting, so doing them
serially made a twenty-source run take minutes.
"""
from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.graph.state import CityResearchState
from app.llm.client import llm_client
from app.llm.json_utils import parse_json_list
from app.models.schemas import (
    DIMENSION_BRIEFS,
    Claim,
    CrawlVerdict,
    ExtractedPassage,
    SourceCandidate,
)
from app.util import domain_of

CLAIM_EXTRACTION_SYSTEM = """You extract atomic factual claims from a passage
about a city, for a public-health research brief.

A claim must be a single, self-contained, checkable statement that a reader
could verify against the source. Prefer claims carrying specifics: named
programmes, named organizations, dates, figures, policy names.

Rules:
- Only extract what the passage actually states. Never infer or embellish.
- Set is_city_level=false when the statement is really about the country or
  region rather than the named city, even if the passage implies otherwise.
- Skip navigation text, boilerplate and marketing copy.
- If the passage contains nothing relevant, return an empty list.

Return ONLY a JSON list: [{"text": "...", "is_city_level": true|false}, ...]"""


# The content types we can turn into prose, and the parser that does it
# reliably. Matched exactly on the MIME type rather than by substring, which
# is what the previous check did and got wrong in both directions: anything
# containing "text" was accepted, so text/csv and text/javascript reached an
# HTML parser and then the model as though they were pages, while an XML feed
# was accepted or rejected depending on which of two equivalent content types
# the server happened to send. text/xml passed and was parsed as HTML — which
# is what bs4's XMLParsedAsHTMLWarning was reporting — and application/xml,
# the same document, was refused.
#
# Note there is deliberately no warnings filter for that warning. It fires
# whenever markup is parsed in the wrong mode, so silencing it would also
# silence the case where a server mislabels XML as text/html and we really
# are using the wrong parser.
_PARSER_FOR_TYPE: dict[str, str | None] = {
    "text/html": "lxml",
    "application/xhtml+xml": "lxml",
    # Feeds and sitemaps. Thin material, but city health departments do
    # publish announcements this way, and "lxml-xml" reads them properly.
    "text/xml": "lxml-xml",
    "application/xml": "lxml-xml",
    "application/rss+xml": "lxml-xml",
    "application/atom+xml": "lxml-xml",
    # Already prose; there is no markup to strip.
    "text/plain": None,
}

_MIN_USABLE_CHARS = 200


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _mock_fetch(url: str) -> str:
    if "resolvetosavelives" in url:
        return (
            "Resolve to Save Lives confirms this city as a partner in its "
            "hypertension control initiative, working alongside the local "
            "ministry of health since 2023 to expand primary-care screening."
        )
    if "health." in url and "gov" in url:
        return (
            "The municipal health department runs a subsidized blood pressure "
            "screening programme at primary health centres across the city, "
            "launched under the national hypertension control umbrella."
        )
    return "No extractable body text (mock fallback)."


def _real_fetch(url: str) -> str:
    import requests
    from bs4 import BeautifulSoup

    resp = requests.get(
        url,
        timeout=settings.HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": "CARDIO4Cities-Research-Bot/0.1"},
    )
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    mime = content_type.split(";")[0].strip().lower()
    if mime not in _PARSER_FOR_TYPE:
        # PDFs and datasets are common on government portals and are worth
        # supporting eventually, but silently feeding their bytes to an LLM
        # produces confident nonsense, so they are skipped and logged instead.
        raise ValueError(f"unsupported content type '{content_type or 'none sent'}'")

    parser = _PARSER_FOR_TYPE[mime]
    if parser is None:
        text = _collapse_whitespace(resp.text)
    else:
        soup = BeautifulSoup(resp.text, parser)
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()

        # An explicit meta robots noindex is the HTML-level equivalent of the
        # X-Robots-Tag header the crawlability agent checks, and can only be
        # seen once the body is in hand — so it is honoured here, at the last
        # moment before the content is used.
        meta = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
        if meta and "noindex" in (meta.get("content") or "").lower():
            raise ValueError("page carries <meta name='robots' content='noindex'>")

        text = _collapse_whitespace(soup.get_text(" "))

    # A page that parsed but yielded almost nothing is not worth a model call:
    # extraction costs one call per source, so an empty page buys an empty
    # answer at full price. Reported rather than passed on silently.
    if len(text) < _MIN_USABLE_CHARS:
        raise ValueError(
            f"only {len(text)} characters of readable text (needs "
            f"{_MIN_USABLE_CHARS})"
        )

    return text[: settings.PASSAGE_CHAR_LIMIT]


def _missing_parser_dependency() -> str | None:
    """Which HTML-parsing dependency is absent, if any.

    Checked once per pass rather than per URL, because this is a fault in the
    environment and not a property of any particular source. Left to surface
    per-source — which is what used to happen — a missing `bs4` produced one
    "Could not read <url>" warning for every candidate: dozens of identical
    lines that read like the entire web had gone down, with the real cause
    (a dependency that was never installed) repeated in each one and obvious
    in none.
    """
    for module, package in (("bs4", "beautifulsoup4"), ("lxml", "lxml")):
        try:
            __import__(module)
        except ImportError:
            return package
    return None


def _claim_id(url: str, index: int) -> str:
    """Stable and unique across retry passes. A per-node counter was not:
    it restarted at zero each pass, so a re-extracted source produced claim
    ids that collided with the first pass's in the audit trail."""
    return f"{domain_of(url)}-{hashlib.sha1(url.encode()).hexdigest()[:8]}-{index}"


def _process_source(candidate: SourceCandidate, city: str) -> tuple[ExtractedPassage | None, list[Claim], str | None]:
    url = candidate.url
    try:
        text = _mock_fetch(url) if settings.is_mock else _real_fetch(url)
    except Exception as exc:
        # A failed fetch is not a fabricated fact. Record why, drop the source.
        return None, [], f"Could not read {url}: {type(exc).__name__}: {exc}"

    if not text.strip():
        return None, [], f"Could not read {url}: page had no extractable text."

    passage = ExtractedPassage(
        url=url,
        domain=domain_of(url),
        title=candidate.title or url,
        text=text,
        dimension=candidate.dimension,
    )

    brief = DIMENSION_BRIEFS.get(candidate.dimension, candidate.dimension)
    user_prompt = (
        f"City under research: {city}\n"
        f"Dimension of interest: {brief}\n"
        f"Source: {candidate.title or url} ({domain_of(url)})\n\n"
        f"Passage:\n{text}"
    )

    try:
        raw = llm_client.complete(CLAIM_EXTRACTION_SYSTEM, user_prompt)
    except Exception as exc:
        # The passage was read successfully, so keep it — it is still indexed
        # for semantic search — but produce no claims and say why.
        return passage, [], f"Could not extract claims from {url}: {type(exc).__name__}: {exc}"

    claims: list[Claim] = []
    for index, item in enumerate(parse_json_list(raw)):
        if not isinstance(item, dict):
            continue
        claim_text = str(item.get("text", "")).strip()
        if not claim_text:
            continue
        claims.append(
            Claim(
                claim_id=_claim_id(url, index),
                text=claim_text,
                source_url=url,
                source_domain=domain_of(url),
                is_city_level=bool(item.get("is_city_level", True)),
                dimension=candidate.dimension,
            )
        )
        if len(claims) >= settings.MAX_CLAIMS_PER_PASSAGE:
            break

    return passage, claims, None


def extraction_node(state: CityResearchState) -> dict:
    city = state["city"]
    candidates_by_url = {c.url: c for c in state.get("candidates", [])}
    allowed_urls = [
        r.url for r in state.get("crawl_results", []) if r.verdict == CrawlVerdict.ALLOWED
    ]

    # Passages accumulate across retries, so re-extracting a URL already
    # processed would duplicate every claim it produced — and with it the
    # audit rows, the graph episodes and the report bullets.
    #
    # `attempted_urls` covers the other half of that, which `passages` alone
    # cannot: a URL whose *fetch failed* yields no passage, so every retry
    # pass used to pick it up again. Three attempts per dead link, and the
    # same failure warning printed three times in the brief.
    done = {p.url for p in state.get("passages", [])} | set(
        state.get("attempted_urls", [])
    )
    todo = [
        candidates_by_url[url]
        for url in allowed_urls
        if url not in done and url in candidates_by_url
    ]
    if not todo:
        return {"passages": [], "claims": []}

    # One environment check for the whole pass, before any network work.
    if not settings.is_mock:
        missing = _missing_parser_dependency()
        if missing:
            return {
                "passages": [],
                "claims": [],
                # Not marked attempted: these URLs were never actually tried,
                # and once the dependency is installed they should be.
                "warnings": [
                    f"No source could be read because '{missing}' is not installed "
                    f"in this environment — every one of the {len(todo)} allowed "
                    f"source(s) would fail identically, so none were fetched. "
                    f"This is a deployment fault, not a research finding: the "
                    f"gaps in this brief mean 'not established', not 'nothing to "
                    f"find'. Fix with `pip install -r requirements.txt`."
                ],
            }

    with ThreadPoolExecutor(max_workers=settings.LLM_MAX_CONCURRENCY) as pool:
        outcomes = list(pool.map(lambda c: _process_source(c, city), todo))

    passages: list[ExtractedPassage] = []
    claims: list[Claim] = []
    warnings: list[str] = []
    for passage, source_claims, warning in outcomes:
        if passage:
            passages.append(passage)
        claims.extend(source_claims)
        if warning:
            warnings.append(warning)

    return {
        "passages": passages,
        "claims": claims,
        "warnings": warnings,
        # Recorded whether or not the fetch worked, which is the point: a URL
        # that failed has still been tried, and a later pass re-trying it
        # spends the same requests to produce the same warning.
        "attempted_urls": [c.url for c in todo],
    }
