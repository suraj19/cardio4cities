"""
The actual LangGraph orchestration.

    planner -> query_gen -> search -> crawlability
                                          |
                          (routing does NOT branch here structurally --
                           crawlability always proceeds to extraction,
                           but extraction only ACTS on ALLOWED urls,
                           enforced in extraction_agent.py itself)
                                          v
                                     extraction -> fact_check -> graph_writer
                                                                      v
                                                             coverage_evaluator
                                                              /              \\
                                                    insufficient          sufficient
                                                   (loop to planner,        v
                                                    bounded by retries)   report -> END

The single conditional edge is deliberate. Branching is a claim that two
paths are genuinely different work; everywhere else the variation is in
what a node *finds*, not in what happens next, and encoding that as graph
structure would make the workflow harder to explain without making it do
anything new. The one place the path really does differ is coverage:
either the brief is good enough to write, or the planner gets another
attempt at the dimensions that came back empty.

Every node is idempotent with respect to a retry — each filters state to
the items it has not already handled — because the retry loop re-enters
nodes over state that accumulates.
"""
from langgraph.graph import StateGraph, END

from app.config import settings
from app.graph.state import CityResearchState
from app.agents.planner import planner_node
from app.agents.query_gen import query_gen_node
from app.agents.search_agent import search_node
from app.agents.crawlability_agent import crawlability_node
from app.agents.extraction_agent import extraction_node
from app.agents.fact_check_agent import fact_check_node
from app.agents.graph_writer_agent import graph_writer_node
from app.agents.coverage_evaluator import coverage_evaluator_node
from app.agents.report_agent import report_node


def _route_after_coverage(state: CityResearchState) -> str:
    return "report" if state.get("coverage_sufficient") else "planner"


def build_workflow():
    graph = StateGraph(CityResearchState)

    graph.add_node("planner", planner_node)
    graph.add_node("query_gen", query_gen_node)
    graph.add_node("search", search_node)
    graph.add_node("crawlability", crawlability_node)
    graph.add_node("extraction", extraction_node)
    graph.add_node("fact_check", fact_check_node)
    graph.add_node("graph_writer", graph_writer_node)
    graph.add_node("coverage_evaluator", coverage_evaluator_node)
    graph.add_node("report", report_node)

    graph.set_entry_point("planner")
    graph.add_edge("planner", "query_gen")
    graph.add_edge("query_gen", "search")
    graph.add_edge("search", "crawlability")
    graph.add_edge("crawlability", "extraction")
    graph.add_edge("extraction", "fact_check")
    graph.add_edge("fact_check", "graph_writer")
    graph.add_edge("graph_writer", "coverage_evaluator")

    graph.add_conditional_edges(
        "coverage_evaluator",
        _route_after_coverage,
        {"planner": "planner", "report": "report"},
    )
    graph.add_edge("report", END)

    return graph.compile()


# Compiled once, reused across requests.
workflow = build_workflow()

# LangGraph counts every node execution against recursion_limit, and its
# default of 25 is almost exactly what three full passes cost (8 nodes per
# pass, plus the report). Deriving the limit from the retry budget means
# raising MAX_PLANNER_RETRIES cannot silently turn into a GraphRecursionError
# two passes in, after several minutes of real research.
NODES_PER_PASS = 8
WORKFLOW_CONFIG = {
    "recursion_limit": NODES_PER_PASS * (settings.MAX_PLANNER_RETRIES + 1) + 5
}
