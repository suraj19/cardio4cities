"""
Search / Discovery Agent.

Turns planned queries into candidate URLs + snippets. This is DISCOVERY
only — no page content is fetched here. That happens later, and only for
URLs that clear the crawlability gate.

Provider priority: Tavily (if key set) -> duckduckgo-search (no key needed)
-> MOCK canned results (no internet needed at all).

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


def _duckduckgo_search(query: PlannedQuery) -> list[SourceCandidate]:
    from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query.text, max_results=5):
            results.append(
                SourceCandidate(
                    url=r.get("href", ""),
                    title=r.get("title", ""),
                    snippet=r.get("body", ""),
                    query_used=query.text,
                    dimension=query.dimension,
                )
            )
    return results


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


def _run_query(query: PlannedQuery, city: str) -> list[SourceCandidate]:
    if settings.is_mock:
        return _mock_search(query, city)
    try:
        if settings.TAVILY_API_KEY:
            return _tavily_search(query)
        return _duckduckgo_search(query)
    except Exception:
        # A provider outage or rate limit on one query should cost that query,
        # not the run. The dimension it served will surface as a gap if no
        # other query covers it.
        return []


def search_node(state: CityResearchState) -> dict:
    city = state["city"]
    queries: list[PlannedQuery] = state.get("planned_queries", [])

    seen_urls = {c.url for c in state.get("candidates", [])}
    per_dimension: dict[str, int] = {}
    deduped: list[SourceCandidate] = []

    for query in queries:
        for candidate in _run_query(query, city):
            if not candidate.url or candidate.url in seen_urls:
                continue
            if per_dimension.get(candidate.dimension, 0) >= settings.MAX_SOURCES_PER_DIMENSION:
                continue
            seen_urls.add(candidate.url)
            per_dimension[candidate.dimension] = per_dimension.get(candidate.dimension, 0) + 1
            deduped.append(candidate)

    return {"candidates": deduped}
