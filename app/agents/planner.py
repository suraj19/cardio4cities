"""
Planner Agent.

Answers the brief's "how should research be planned?" in two parts.

Decomposition: "understand a city" is split into the five dimensions in
schemas.DIMENSION_BRIEFS, chosen to match what a City Lead actually needs
before a stakeholder meeting — burden, programmes, policy, stakeholders,
system capacity. Fixing the dimension set (rather than letting an LLM
invent one per city) is what makes two cities comparable, and what lets
the coverage evaluator say "we know nothing about policy here" instead of
only "we found twelve facts".

Re-planning: on a retry the planner does not repeat itself. It narrows
`focus_dimensions` to the dimensions that came back empty, so the second
pass spends its whole budget on the gaps rather than re-researching what
already succeeded. That also makes retries progressively cheaper.
"""
from app.graph.state import CityResearchState
from app.models.schemas import DIMENSIONS


def planner_node(state: CityResearchState) -> dict:
    retry_count = state.get("retry_count", 0)

    if retry_count == 0:
        return {
            "dimensions": DIMENSIONS,
            "focus_dimensions": DIMENSIONS,
            "retry_count": 0,
        }

    # Coverage was judged insufficient. Re-plan against the shortfall only;
    # fall back to everything if the evaluator somehow named nothing.
    uncovered = state.get("uncovered_dimensions") or state.get("dimensions", DIMENSIONS)
    return {
        "focus_dimensions": list(uncovered),
        "retry_count": retry_count,
    }
