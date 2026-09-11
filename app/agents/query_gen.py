"""
Query Generation Agent.

Turns each in-scope dimension into concrete search-engine queries. One
LLM call covers every dimension at once — five separate calls would cost
five round trips to produce what is essentially one planning decision.

The prompt pushes hard toward primary sources (government health
departments, WHO, NGO programme pages) because source quality is decided
here, before the crawlability gate ever sees a URL. A query that returns
listicles cannot be rescued downstream.

On a retry the state's gap list is included so the model can rephrase
around what previously came back empty, and queries already tried are
excluded so a retry cannot burn its budget re-running the same search.

This node degrades rather than fails. It is the first LLM call in the run,
so an unreachable provider here would otherwise kill a run that could still
have produced a useful "here is what we could not establish" brief. Both
failure modes — the model answering with unusable JSON, and the call not
completing at all — land on the same keyword templates below.
"""
from app.config import settings
from app.graph.state import CityResearchState
from app.llm.client import llm_client
from app.llm.json_utils import parse_json_object
from app.models.schemas import DIMENSION_BRIEFS, PlannedQuery

SYSTEM_PROMPT = """You generate web search queries for a city research pipeline
that briefs public-health teams before they meet city government stakeholders.

For each research dimension you are given, write short, specific search queries
likely to surface PRIMARY sources: municipal or national health department
pages, ministry publications, WHO or PAHO pages, NGO and funder programme
pages, official statistics portals, and peer-reviewed studies. Avoid queries
that mainly return news aggregators, blogs or listicles.

Always include the city name in every query.

Return ONLY a JSON object mapping each dimension key to a list of query
strings, like {"dimension_key": ["query one", "query two"]}."""


def _fallback_queries(city: str, dimension: str) -> list[str]:
    """Used when the model returns nothing usable for a dimension, or does not
    answer at all. Keyword templates are worse than generated queries but they
    are never empty, and a dimension with no queries is a guaranteed gap."""
    templates = {
        "cv_burden": [
            f"{city} hypertension prevalence statistics",
            f"{city} cardiovascular disease burden report",
        ],
        "healthcare_programmes": [
            f"{city} hypertension screening programme health department",
            f"{city} diabetes prevention initiative city government",
        ],
        "policy_initiatives": [
            f"{city} noncommunicable disease action plan policy",
            f"{city} municipal health strategy cardiovascular",
        ],
        "stakeholders": [
            f"{city} municipal health department partners NGO",
            f"{city} hospitals universities public health organizations",
        ],
        "health_system": [
            f"{city} primary health centres capacity health system",
            f"{city} health workforce medicines availability report",
        ],
    }
    return templates.get(dimension, [f"{city} {dimension.replace('_', ' ')}"])


def _plan_with_model(user_prompt: str) -> tuple[dict, str | None]:
    """Returns the parsed plan and, if the call failed, a warning to record.

    An empty dict is the right degraded value: the loop below already treats
    "no queries for this dimension" as the trigger for the keyword templates,
    so a dead provider needs no separate code path — it just means every
    dimension takes the fallback.
    """
    try:
        return parse_json_object(llm_client.complete(SYSTEM_PROMPT, user_prompt)), None
    except Exception as exc:
        return {}, (
            f"Query planning fell back to keyword templates: the LLM call failed "
            f"({type(exc).__name__}: {exc}). Searches for this pass are generic "
            f"rather than planned, so coverage is likely to be narrower."
        )


def query_gen_node(state: CityResearchState) -> dict:
    city = state["city"]
    country = state.get("country")
    focus = state.get("focus_dimensions") or state.get("dimensions") or list(DIMENSION_BRIEFS)
    retry_count = state.get("retry_count", 0)

    location = f"{city}, {country}" if country else city

    brief_block = "\n".join(
        f"- {key}: {DIMENSION_BRIEFS[key]}" for key in focus if key in DIMENSION_BRIEFS
    )

    gap_context = ""
    prior_gaps = state.get("gaps", [])
    if retry_count > 0 and prior_gaps:
        gap_descriptions = [g.description for g in prior_gaps][:8]
        gap_context = (
            "\n\nA previous pass left these unresolved. Rephrase around them — "
            "try official portal names, local-language terms, or the responsible "
            f"institution rather than the topic:\n{gap_descriptions}"
        )

    user_prompt = (
        f"City: {location}\n"
        f"Dimensions to cover:\n{brief_block}\n"
        f"Write up to {settings.QUERIES_PER_DIMENSION} queries per dimension."
        f"{gap_context}"
    )

    parsed, warning = _plan_with_model(user_prompt)

    # `planned_queries` only holds the previous pass, but `candidates`
    # accumulates across all of them and records the query that found each
    # one, so together they cover the full history of what has been searched.
    already_used = {q.text.lower() for q in state.get("planned_queries", [])}
    already_used |= {c.query_used.lower() for c in state.get("candidates", []) if c.query_used}

    planned: list[PlannedQuery] = []
    for dimension in focus:
        raw_queries = parsed.get(dimension)
        candidates = [q for q in raw_queries if isinstance(q, str) and q.strip()] if isinstance(
            raw_queries, list
        ) else []
        if not candidates:
            candidates = _fallback_queries(city, dimension)

        kept = 0
        for query in candidates:
            text = query.strip()
            if text.lower() in already_used:
                continue
            already_used.add(text.lower())
            planned.append(PlannedQuery(text=text, dimension=dimension))
            kept += 1
            if kept >= settings.QUERIES_PER_DIMENSION:
                break

    # `planned_queries` is overwritten, not appended: this is the plan for THIS
    # pass. Candidates and passages accumulate; the query list should not, or a
    # third pass would re-search everything the first pass already covered.
    # `warnings` does accumulate, so a per-pass failure stays on the record.
    return {
        "planned_queries": planned,
        "warnings": [warning] if warning else [],
    }
