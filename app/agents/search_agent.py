"""
Search / Discovery Agent.

Turns planned queries into candidate URLs + snippets. This is DISCOVERY
only — no page content is fetched here. That happens later, and only for
URLs that clear the crawlability gate.

Provider priority: Tavily (if key set) -> ddgs (no key needed) -> MOCK canned
results (no internet needed at all).

A failing provider costs the query it hit and no more, but it is *reported*.
That is not cosmetic: discovery is the one stage whose failure is otherwise
indistinguishable from a genuine absence of information, because a run with
no candidates and a run about an undocumented city produce the same gaps.

Two forms of pruning happen here rather than downstream, because every
surviving URL costs a robots.txt fetch, a page fetch and an LLM call:

  * URLs already seen in an earlier pass are dropped, so a retry cannot
    re-research what it already has.
  * Each dimension keeps at most MAX_SOURCES_PER_DIMENSION candidates, so
    one dimension with abundant coverage cannot crowd out the budget of a
    dimension with sparse coverage. Capping per dimension rather than in
    total is what keeps a five-dimension brief balanced.
"""
from app.config import settings
from app.graph.state import CityResearchState
from app.models.schemas import PlannedQuery, SourceCandidate


def _mock_search(query: PlannedQuery, city: str) -> list[SourceCandidate]:
    """Deterministic canned results so the graph is runnable with zero internet.

    Deliberately includes a mix of source types (gov, NGO, news, a
    LinkedIn-style URL) so the crawlability agent downstream has something
    real to differentiate between, even in mock mode.
    """
    root = city.lower().replace(" ", "")
    dim = query.dimension
    return [
        SourceCandidate(
            url=f"https://health.{root}.gov/{dim}",
            title=f"{city} Health Department — {dim.replace('_', ' ')}",
            snippet=(
                f"The {city} municipal health department publishes information on "
                f"{dim.replace('_', ' ')}, including a subsidized blood pressure "
                f"screening programme across primary health centres."
            ),
            query_used=query.text,
            dimension=dim,
        ),
        SourceCandidate(
            url=f"https://resolvetosavelives.org/{dim}/{root}",
            title=f"Resolve to Save Lives — {city} {dim.replace('_', ' ')}",
            snippet=(
                f"Resolve to Save Lives lists {city} as a partner city for its "
                f"hypertension control initiative, launched in partnership with "
                f"the local ministry of health."
            ),
            query_used=query.text,
            dimension=dim,
        ),
        SourceCandidate(
            url=f"https://localnews.example.com/{root}-{dim}-2025",
            title=f"{city} announces new diabetes screening drive",
            snippet=(
                f"Local officials in {city} announced an expanded diabetes "
                f"screening drive, according to a press briefing."
            ),
            query_used=query.text,
            dimension=dim,
        ),
        SourceCandidate(
            url=f"https://www.linkedin.com/company/{root}-health-dept",
            title=f"{city} Health Department | LinkedIn",
            snippet="Company page.",
            query_used=query.text,
            dimension=dim,
        ),
    ]


def _ddgs_class():
    """Resolve the DDGS class across the package rename.

    `duckduckgo-search` was renamed to `ddgs`, and recent releases of the old
    name raise on import rather than merely warning. Trying the new name first
    means a fresh install gets the maintained package, while an existing
    virtualenv holding the old one keeps working instead of silently
    returning no search results.
    """
    try:
        from ddgs import DDGS

        return DDGS
    except ImportError:
        pass
    try:
        from duckduckgo_search import DDGS  # legacy name, pre-rename

        return DDGS
    except ImportError as exc:
        raise ImportError(
            "No DuckDuckGo client is installed. Run `pip install -U ddgs` — "
            "the `duckduckgo-search` package was renamed to `ddgs`."
        ) from exc


def _duckduckgo_search(query: PlannedQuery) -> list[SourceCandidate]:
    # Deliberately not used as a context manager. `ddgs.DDGS` does not
    # implement that protocol, and `with` on it raises an AttributeError that
    # would reach the caller's handler looking exactly like a search outage.
    client = _ddgs_class()(timeout=settings.HTTP_TIMEOUT_SECONDS)
    return [
        SourceCandidate(
            url=r.get("href", ""),
            title=r.get("title", ""),
            snippet=r.get("body", ""),
            query_used=query.text,
            dimension=query.dimension,
        )
        # `text()` returns title/href/body under exactly those keys in both
        # the legacy package and `ddgs`, so this parsing is version-agnostic.
        # Only arguments both versions accept are passed, which is what keeps
        # the fallback import above usable.
        for r in client.text(query.text, max_results=5)
    ]


def _tavily_search(query: PlannedQuery) -> list[SourceCandidate]:
    from tavily import TavilyClient

    client = TavilyClient(api_key=settings.TAVILY_API_KEY)
    response = client.search(query.text, max_results=5)
    return [
        SourceCandidate(
            url=r["url"],
            title=r.get("title", ""),
            snippet=r.get("content", ""),
            query_used=query.text,
            dimension=query.dimension,
        )
        for r in response.get("results", [])
    ]


def _run_query(query: PlannedQuery, city: str) -> tuple[list[SourceCandidate], str | None]:
    """Returns the candidates and, if the provider failed, a warning to record.

    The warning is the whole point of this signature. This function used to
    swallow the exception and return an empty list, which made a broken search
    provider indistinguishable from a city nobody has written about: the only
    symptom was the coverage evaluator's *"Search returned no candidate
    sources"*, and the actual cause — a rate limit, a renamed dependency, no
    egress — appeared nowhere at all. A provider failure still costs only the
    query it hit, but now it says so out loud.
    """
    if settings.is_mock:
        return _mock_search(query, city), None

    provider = "Tavily" if settings.TAVILY_API_KEY else "DuckDuckGo"
    try:
        if settings.TAVILY_API_KEY:
            return _tavily_search(query), None
        return _duckduckgo_search(query), None
    except Exception as exc:
        # Keyed on the error rather than on the query text, so that ten queries
        # failing the same way collapse into one warning instead of ten.
        return [], f"{provider} search failed ({type(exc).__name__}: {exc})."


def search_node(state: CityResearchState) -> dict:
    city = state["city"]
    queries: list[PlannedQuery] = state.get("planned_queries", [])

    seen_urls = {c.url for c in state.get("candidates", [])}
    per_dimension: dict[str, int] = {}
    deduped: list[SourceCandidate] = []
    failures: list[str] = []

    for query in queries:
        results, warning = _run_query(query, city)
        if warning:
            failures.append(warning)
        for candidate in results:
            if not candidate.url or candidate.url in seen_urls:
                continue
            if per_dimension.get(candidate.dimension, 0) >= settings.MAX_SOURCES_PER_DIMENSION:
                continue
            seen_urls.add(candidate.url)
            per_dimension[candidate.dimension] = per_dimension.get(candidate.dimension, 0) + 1
            deduped.append(candidate)

    # dict.fromkeys collapses the repeats while preserving order.
    warnings = list(dict.fromkeys(failures))
    if queries and len(failures) == len(queries):
        # Total discovery failure needs saying separately, because the gaps it
        # produces are otherwise worded as though the research was done and
        # found nothing. That distinction is the difference between "we do not
        # know" and "there is nothing to know".
        warnings.append(
            f"All {len(queries)} search queries failed this pass, so no sources "
            f"could be discovered. This is a search provider or dependency "
            f"problem, not an absence of information about {city} — the gaps "
            f"below mean 'not established', not 'nothing to find'."
        )

    return {"candidates": deduped, "warnings": warnings}
