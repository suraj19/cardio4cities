"""
FastAPI app exposing the research pipeline and the intelligence asset it
builds.

  POST /research            run the full LangGraph pipeline for a city
  POST /ask                 grounded, cited answer over vector + graph
  GET  /cities              cities already researched (the reusable asset)
  GET  /report/{city}       the stored brief as JSON
  GET  /report/{city}/download   the brief as a downloadable .md file
  GET  /sources/{city}      every URL considered + the crawl gate's verdict
  GET  /facts/{city}        fact audit trail: tier, reasoning, provenance
  GET  /graph/{city}        knowledge-graph facts with temporal validity
  GET  /health              liveness check

The read endpoints exist so that "where did this come from?" is answerable
from outside the run that produced the answer. A report returned only in
the POST response would make provenance a property of one HTTP call
rather than of the system.
"""
import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app.graph.workflow import WORKFLOW_CONFIG, workflow
from app.llm.client import llm_client
from app.models.schemas import ConfidenceTier, CrawlVerdict, ResearchRequest, dimension_label
from app.stores.graph_store import graph_store
from app.stores.relational_store import relational_store
from app.stores.vector_store import vector_store

# Uvicorn configures its own loggers and leaves the root logger at WARNING, so
# without this the pipeline's per-node progress lines are created and then
# discarded — the exact symptom is a terminal that prints the access log line
# for POST /research minutes after it prints nothing else, which reads as a
# hang. basicConfig only installs a handler when the root has none, which is
# the case under uvicorn, so it adds our output without disturbing its.
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(title="CARDIO4Cities City Intelligence")

# The UI is served from this same app in deployment, so CORS is not needed for
# the normal path. It stays permissive so the page also works when opened
# straight off disk (which sends Origin: null) during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ASK_SYSTEM = """You are a city intelligence assistant for public-health teams.

Answer the user's question using ONLY the numbered evidence provided. Cite the
evidence you use inline as [1], [2], and so on, immediately after the statement
it supports.

If the evidence does not answer the question, say so plainly and name what is
missing. Never supply a figure, name, date or organization that is not in the
evidence. Never present national or regional information as city-specific.
Keep the answer under 150 words."""


class AskRequest(BaseModel):
    city: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=1000)


def _clean_city(city: str) -> str:
    city = city.strip()
    if not city:
        raise HTTPException(status_code=400, detail="City must not be empty.")
    return city[:120]


@app.get("/health")
def health():
    return {"status": "ok", "run_mode": settings.RUN_MODE}


@app.post("/research")
async def research(req: ResearchRequest):
    city = _clean_city(req.city)
    final_state = await workflow.ainvoke(
        {"city": city, "country": req.country, "retry_count": 0},
        config=WORKFLOW_CONFIG,
    )

    fact_checked = final_state.get("fact_checked", [])
    crawl_results = final_state.get("crawl_results", [])

    def tier_count(tier: ConfidenceTier) -> int:
        return sum(1 for f in fact_checked if f.tier == tier)

    return {
        "city": city,
        "dimensions": final_state.get("dimensions", []),
        "covered_dimensions": final_state.get("covered_dimensions", []),
        "uncovered_dimensions": final_state.get("uncovered_dimensions", []),
        "research_passes": final_state.get("retry_count", 0) + 1,
        "report_markdown": final_state.get("report_markdown"),
        "warnings": final_state.get("warnings", []),
        "counts": {
            "candidates_considered": len(crawl_results),
            "sources_allowed": sum(
                1 for r in crawl_results if r.verdict == CrawlVerdict.ALLOWED
            ),
            "sources_denied": sum(
                1 for r in crawl_results if r.verdict == CrawlVerdict.DENIED
            ),
            "sources_read": len(final_state.get("passages", [])),
            "verified_facts": tier_count(ConfidenceTier.VERIFIED),
            "single_source_facts": tier_count(ConfidenceTier.SINGLE_SOURCE),
            "conflicting_facts": tier_count(ConfidenceTier.CONFLICTING),
            "unsupported_claims": tier_count(ConfidenceTier.UNSUPPORTED),
            "gaps": len(final_state.get("gaps", [])),
        },
    }


@app.get("/cities")
def cities():
    return {"cities": relational_store.list_cities()}


@app.get("/report/{city}")
def get_report(city: str):
    report = relational_store.get_report(_clean_city(city))
    if not report:
        raise HTTPException(
            status_code=404, detail=f"No report stored for '{city}' — run /research first."
        )
    return report


@app.get("/report/{city}/download")
def download_report(city: str):
    city = _clean_city(city)
    report = relational_store.get_report(city)
    if not report:
        raise HTTPException(
            status_code=404, detail=f"No report stored for '{city}' — run /research first."
        )
    filename = f"cardio4cities-{city.lower().replace(' ', '-')}-brief.md"
    return Response(
        content=report["markdown"],
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/sources/{city}")
def get_sources(city: str):
    """The crawlability gate's full record — including what it refused. The
    denied rows are the point: they show the gate ran."""
    sources = relational_store.get_sources(_clean_city(city))
    if not sources:
        raise HTTPException(
            status_code=404, detail=f"No sources recorded for '{city}' — run /research first."
        )
    return {"city": city, "count": len(sources), "sources": sources}


@app.get("/facts/{city}")
def get_facts(city: str):
    city = _clean_city(city)
    facts = relational_store.get_facts(city)
    if not facts:
        raise HTTPException(
            status_code=404, detail=f"No facts recorded for '{city}' — run /research first."
        )
    for fact in facts:
        fact["dimension_label"] = dimension_label(fact["dimension"])
    return {
        "city": city,
        "facts": facts,
        "gaps": relational_store.get_gaps(city),
    }


@app.get("/graph/{city}")
async def get_graph(city: str, question: str | None = None, limit: int = Query(15, ge=1, le=50)):
    """Reads the knowledge graph at query time.

    Graphiti's hybrid search over the Neo4j Sandbox returns the relationship
    edges it extracted from the fact episodes, each with the validity window
    that makes the graph a record over time rather than a snapshot. Passing
    `question` scopes the traversal to what the user is actually asking.
    """
    city = _clean_city(city)
    try:
        facts = await graph_store.query_facts_for_city(city, question=question, limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Knowledge graph unavailable ({type(exc).__name__}: {exc}). "
            f"If this is LIVE mode, check the Neo4j Sandbox has not expired.",
        )
    return {"city": city, "question": question, "count": len(facts), "facts": facts}


@app.post("/ask")
async def ask(req: AskRequest):
    """Conversational retrieval over two of the three stores.

    The vector store supplies passage text for open-ended recall; the graph
    supplies extracted relationships, which answer "how do these things
    connect" in a way nearest-neighbour search over prose cannot. Both are
    numbered into one evidence block so the model's citations map back to
    real URLs and graph edges, and the answer is refused outright when the
    evidence does not support one.
    """
    city = _clean_city(req.city)

    passages, graph_facts, degraded = [], [], []
    try:
        # Embedding the question is CPU-bound and local, so it also goes to a
        # worker thread rather than stalling the loop. The lambda matters:
        # `vector_store` is lazy, so touching `.query` is what loads the
        # embedding model the first time. Passing the bound method directly
        # would do that load on the event loop before the thread ever starts.
        passages = await asyncio.to_thread(lambda: vector_store.query(city, req.question))
    except Exception as exc:
        degraded.append(f"Vector store unavailable ({type(exc).__name__}).")
    try:
        graph_facts = await graph_store.query_facts_for_city(city, question=req.question, limit=8)
    except Exception as exc:
        degraded.append(f"Knowledge graph unavailable ({type(exc).__name__}).")

    if not passages and not graph_facts:
        raise HTTPException(
            status_code=404,
            detail=f"Nothing indexed for '{city}' yet — run /research first. "
            + " ".join(degraded),
        )

    citations, blocks = [], []
    for passage in passages:
        citations.append(
            {
                "n": len(citations) + 1,
                "type": "source_passage",
                "url": passage.get("url"),
                "title": passage.get("title"),
                "dimension": passage.get("dimension"),
            }
        )
        blocks.append(f"[{len(citations)}] (web source: {passage.get('url')})\n{passage.get('text', '')[:1200]}")

    for fact in graph_facts:
        citations.append(
            {
                "n": len(citations) + 1,
                "type": "graph_fact",
                "relationship": fact.get("relationship"),
                "valid_at": fact.get("valid_at"),
            }
        )
        blocks.append(f"[{len(citations)}] (knowledge graph fact)\n{fact.get('fact', '')}")

    # The LLM client is synchronous; off-loading it keeps one slow answer from
    # blocking the event loop for every other request.
    answer = await asyncio.to_thread(
        llm_client.complete,
        ASK_SYSTEM,
        f"City: {city}\nQuestion: {req.question}\n\nEvidence:\n" + "\n\n".join(blocks),
    )

    return {
        "city": city,
        "question": req.question,
        "answer": answer.strip(),
        "citations": citations,
        "degraded": degraded,
    }


# Mounted last so the API routes above take precedence. Serving the UI from
# the same origin as the API is what makes this a single deployable URL.
_frontend = Path(__file__).resolve().parent.parent / "frontend"
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
