"""
The actual LangGraph orchestration.

    planner -> query_gen -> search -> official_data -> crawlability
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

`official_data` sits between search and crawlability rather than beside
them because it reaches the rest of the pipeline entirely through the
existing idempotency guards. It emits its own ALLOWED crawl verdicts, so
crawlability skips its URLs instead of fetching robots.txt from an API
host; and it emits its own passages, so extraction skips them instead of
re-fetching JSON and handing it to a language model. Nothing downstream
needed changing to accommodate it.
"""
import inspect
import logging
import time

from langgraph.graph import StateGraph, END

from app.config import settings
from app.graph.state import CityResearchState
from app.telemetry import record_node_duration, span
from app.agents.planner import planner_node
from app.agents.query_gen import query_gen_node
from app.agents.search_agent import search_node
from app.agents.official_data_agent import official_data_node
from app.agents.crawlability_agent import crawlability_node
from app.agents.extraction_agent import extraction_node
from app.agents.fact_check_agent import fact_check_node
from app.agents.graph_writer_agent import graph_writer_node
from app.agents.coverage_evaluator import coverage_evaluator_node
from app.agents.report_agent import report_node


logger = logging.getLogger(__name__)

# Declared as data so the recursion limit below can be derived from it. The
# order is the execution order, for readability only — the edges are what
# actually sequence the run.
NODES = {
    "planner": planner_node,
    "query_gen": query_gen_node,
    "search": search_node,
    "official_data": official_data_node,
    "crawlability": crawlability_node,
    "extraction": extraction_node,
    "fact_check": fact_check_node,
    "graph_writer": graph_writer_node,
    "coverage_evaluator": coverage_evaluator_node,
    "report": report_node,
}


def _summarise(result) -> str:
    """What a node produced, as short counts."""
    if not isinstance(result, dict):
        return ""
    counts = [f"{key} +{len(value)}" for key, value in result.items()
              if isinstance(value, list) and value]
    return ", ".join(counts) if counts else "nothing new"


def _job_of(config) -> object | None:
    """The research job this run belongs to, if it was started as one.

    Reached through LangGraph's `configurable` rather than a module global so
    two concurrent runs report to their own job, and so the workflow keeps
    knowing nothing about app.jobs — the dependency points one way, which is
    what lets the graph still be driven straight from a test with no job at
    all. A plain `workflow.ainvoke(...)` passes no job and every call below
    is a no-op.
    """
    if not isinstance(config, dict):
        return None
    return (config.get("configurable") or {}).get("job")


def _timed(name: str, node):
    """Log a node's start, duration and output size, and report both to the job.

    A city takes minutes, almost all of it inside two nodes, and the server
    used to log nothing at all between accepting POST /research and answering
    it. A run in progress was therefore indistinguishable from a hung one,
    which is not a hypothetical confusion — it is the reading anyone would
    take from a silent terminal. Timings also make the expensive node
    obvious rather than a matter of opinion: it is normally graph_writer,
    because Graphiti runs its own extraction for every fact written.

    The same two lines now also drive `GET /research/{job_id}`, so the
    progress a developer reads in the log and the progress a user sees in the
    browser are the same measurement rather than two that can disagree.

    The wrapper's second parameter must be named `config`: that is the name
    LangGraph looks for when deciding whether a node wants the run config.
    """
    def _before(state, config):
        logger.info("[%s] %s ...", state.get("city", "?"), name)
        job = _job_of(config)
        if job is not None:
            job.stage_started(name)
        return time.perf_counter()

    def _after(state, config, started, result):
        seconds = time.perf_counter() - started
        summary = _summarise(result)
        logger.info(
            "[%s] %s finished in %.1fs (%s)",
            state.get("city", "?"), name, seconds, summary,
        )
        job = _job_of(config)
        if job is not None:
            job.stage_finished(name, seconds, summary)
        return seconds

    # The span wraps the node rather than sitting inside `_before`/`_after`,
    # because a context manager is the only shape that still closes when the
    # node raises — and it is the raising runs that are worth looking at.
    # Everything the log line says goes on the span too, so a trace and a log
    # of the same run cannot disagree.
    if inspect.iscoroutinefunction(node):
        async def wrapper(state, config=None):
            with span(f"node.{name}", **{
                "cardio4cities.node": name,
                "cardio4cities.city": state.get("city"),
                "cardio4cities.pass": state.get("pass_number"),
            }) as current:
                started = _before(state, config)
                result = await node(state)
                seconds = _after(state, config, started, result)
                current.set_attribute("cardio4cities.node.summary", _summarise(result))
                record_node_duration(name, seconds)
                return result
    else:
        def wrapper(state, config=None):
            with span(f"node.{name}", **{
                "cardio4cities.node": name,
                "cardio4cities.city": state.get("city"),
                "cardio4cities.pass": state.get("pass_number"),
            }) as current:
                started = _before(state, config)
                result = node(state)
                seconds = _after(state, config, started, result)
                current.set_attribute("cardio4cities.node.summary", _summarise(result))
                record_node_duration(name, seconds)
                return result

    wrapper.__name__ = f"timed_{name}"
    return wrapper


def _route_after_coverage(state: CityResearchState) -> str:
    return "report" if state.get("coverage_sufficient") else "planner"


def build_workflow():
    graph = StateGraph(CityResearchState)

    for name, node in NODES.items():
        graph.add_node(name, _timed(name, node))

    graph.set_entry_point("planner")
    graph.add_edge("planner", "query_gen")
    graph.add_edge("query_gen", "search")
    graph.add_edge("search", "official_data")
    graph.add_edge("official_data", "crawlability")
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
# default of 25 is almost exactly what three full passes cost. Deriving the
# limit from the retry budget means raising MAX_PLANNER_RETRIES cannot
# silently turn into a GraphRecursionError two passes in, after several
# minutes of real research.
#
# Derived from NODES rather than written down: `report` runs once at the end,
# every other node runs once per pass. This used to be a hand-maintained
# constant with a comment warning that adding a node meant updating it, which
# is a comment asking a human to do something a subtraction can do.
NODES_PER_PASS = len(NODES) - 1
WORKFLOW_CONFIG = {
    "recursion_limit": NODES_PER_PASS * (settings.MAX_PLANNER_RETRIES + 1) + 5
}
