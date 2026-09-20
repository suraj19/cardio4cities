"""
FastAPI app exposing the research pipeline and the intelligence asset it
builds.

  POST   /research            start a research job; returns an id to poll
  GET    /research/{job_id}   job status, per-node progress, brief when done
  DELETE /research/{job_id}   cancel an in-flight job
  GET    /research            recent jobs
  POST   /ask                 grounded, cited answer over vector + graph
  GET    /cities              cities already researched (the reusable asset)
  GET    /report/{city}       the stored brief as JSON
  GET    /report/{city}/download   the brief as a downloadable .md file
  GET    /sources/{city}      every URL considered + the crawl gate's verdict
  GET    /facts/{city}        fact audit trail: tier, reasoning, provenance
  GET    /graph/{city}        knowledge-graph facts with temporal validity
  GET    /health              liveness check

Research is the only asynchronous endpoint, and it has to be: a run is
minutes long, and holding an HTTP request open across that means betting the
deliverable on a proxy that will not wait — see app/jobs.py for the specific
deadlines. Everything else answers from a store and returns immediately.

The read endpoints exist so that "where did this come from?" is answerable
from outside the run that produced the answer. A report returned only in
the POST response would make provenance a property of one HTTP call
rather than of the system.
"""
import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import telemetry
from app.config import settings
from app.jobs import TERMINAL_STATES, registry
from app.llm.client import llm_client
from app.models.schemas import ResearchRequest, dimension_label
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

logger = logging.getLogger(__name__)


def _prewarm_embeddings() -> None:
    """Load the local embedding model before anyone asks for it.

    ~60s on a cold container, and it used to land on the first request that
    touched the vector store or the graph — which during a demo reads as the
    app having hung, and on a host with an HTTP deadline can consume the whole
    request budget before any work starts. Doing it at startup moves that cost
    somewhere nobody is waiting.

    In a thread rather than awaited, because the healthcheck has to start
    answering immediately: a platform that probes `/health` during startup and
    gets no response will conclude the deploy failed and roll it back.
    """
    try:
        from app.llm import embeddings

        embeddings.get_model()
        logger.info("Embedding model %s loaded.", settings.EMBEDDING_MODEL)
    except Exception as exc:
        # Not fatal. The model is loaded lazily anyway, so the only loss is
        # the head start — and an image built without the model baked in can
        # legitimately fail here with no network.
        logger.warning(
            "Could not pre-load the embedding model (%s: %s). It will load on "
            "first use instead.", type(exc).__name__, exc,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before anything else in startup: FastAPI instrumentation has to attach
    # to the app, and a node that runs before the tracer exists produces no
    # span. Cheap and silent when OTEL_ENABLED is unset, which is the default.
    telemetry.setup(app)
    if settings.PREWARM_EMBEDDINGS and not settings.is_mock:
        threading.Thread(
            target=_prewarm_embeddings, name="prewarm-embeddings", daemon=True
        ).start()
    yield


app = FastAPI(title="CARDIO4Cities City Intelligence", lifespan=lifespan)

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


def _shared_dependency_hint(outages: list[str]) -> str:
    """Name the embedding model when it is the thing both stores tripped over.

    The vector store and the knowledge graph look independent — different
    databases, different hosts — but they share one component: the local
    embedding model. Milvus needs it to embed the question, and Graphiti's
    retrieval embeds the question too, through the same LocalEmbedder. So a
    model that cannot load takes out both at once and presents as two
    unrelated outages, which sends the reader off checking two healthy
    databases.

    Detected by both failures carrying the same exception type, which is what
    a shared dependency looks like from here.
    """
    if len(outages) < 2:
        return ""
    kinds = {failure.split("(")[-1].split(":")[0] for failure in outages}
    if len(kinds) != 1:
        return ""
    return (
        " Both failed the same way, which usually means the shared dependency "
        "rather than either database: the local embedding model. It is "
        "downloaded on first use, so check network egress to huggingface.co "
        "(a TLS interception proxy shows up here as a certificate error) and "
        "that `fastembed` is installed in the interpreter running uvicorn. "
        "`python -m scripts.check_services` checks both."
    )


@app.get("/health")
def health():
    return {"status": "ok", "run_mode": settings.RUN_MODE}


@app.post("/research", status_code=202)
async def start_research(req: ResearchRequest, response: Response):
    """Register a research job and return immediately.

    202 rather than 200, because nothing has been researched yet — the body
    is a receipt, not a result. Poll `GET /research/{job_id}` for progress and
    for the brief.

    A second request for a city already being researched returns the job
    already running instead of starting another. Two concurrent passes over
    one city would write the same facts twice and spend the overlap
    contending for the SQLite write lock, and the usual way to trigger it is
    a double-click on Run.
    """
    city = _clean_city(req.city)

    # The check and the insert happen together inside `submit`, under one
    # lock. Doing it here in two awaits left a window in which two clicks
    # could both start a run.
    job, created = await registry.submit(city, req.country)
    response.headers["Location"] = f"/research/{job.job_id}"
    payload = job.as_dict(include_result=False)
    if not created:
        payload["note"] = f"A research job for '{city}' is already {job.state}."
    return payload


@app.get("/research")
async def list_research():
    """Recent jobs, newest first. Results are omitted — a brief is large and
    this is a list view; fetch the individual job for the report."""
    return {"jobs": await registry.list_jobs()}


@app.get("/research/{job_id}")
async def get_research(
    job_id: str,
    wait: int = Query(
        0,
        ge=0,
        le=60,
        description="Seconds to hold the response open waiting for the job to "
        "finish. 0 returns the current state immediately.",
    ),
):
    """Job status, per-node progress, and the brief once it exists.

    `wait` makes this a bounded long-poll, which exists for scripts and the
    `curl` examples in the docs: without it, a shell caller has to implement
    a sleep loop to do the obvious thing. It is capped at 60 seconds — well
    inside every proxy deadline named in app/jobs.py — so a caller that wants
    to block for the whole run still has to loop, but loops once a minute
    instead of once a second.
    """
    job = await registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"No job '{job_id}'. Finished jobs are kept for "
            f"{settings.JOB_RETENTION_SECONDS}s; the brief itself is stored "
            f"permanently, so try GET /report/{{city}}.",
        )

    deadline = asyncio.get_running_loop().time() + wait
    while job.state not in TERMINAL_STATES:
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.5)

    return job.as_dict()


@app.delete("/research/{job_id}")
async def cancel_research(job_id: str):
    """Cancel an in-flight job.

    Stops the pipeline between nodes rather than mid-node: work already
    handed to a thread pool runs to completion, and anything a node persisted
    before the cancel stays persisted. That is deliberate — a half-written
    audit trail is still a true record of what was read.
    """
    job = await registry.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job '{job_id}'.")
    return job.as_dict(include_result=False)


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
    # Kept separate from `degraded` because these two lists answer different
    # questions. `degraded` tells a caller who got an answer what was missing
    # from it; `outages` decides whether an empty result means "nothing has
    # been researched" or "we could not look".
    outages = []
    try:
        # Embedding the question is CPU-bound and local, so it also goes to a
        # worker thread rather than stalling the loop. The lambda matters:
        # `vector_store` is lazy, so touching `.query` is what loads the
        # embedding model the first time. Passing the bound method directly
        # would do that load on the event loop before the thread ever starts.
        passages = await asyncio.to_thread(lambda: vector_store.query(city, req.question))
    except Exception as exc:
        outages.append(f"Vector store unavailable ({type(exc).__name__}: {exc}).")
        degraded.append(f"Vector store unavailable ({type(exc).__name__}).")
    try:
        graph_facts = await graph_store.query_facts_for_city(city, question=req.question, limit=8)
    except Exception as exc:
        outages.append(f"Knowledge graph unavailable ({type(exc).__name__}: {exc}).")
        degraded.append(f"Knowledge graph unavailable ({type(exc).__name__}).")

    if not passages and not graph_facts:
        # 503 rather than 404 when both stores raised. They are not the same
        # situation and they need opposite responses: 404 tells the operator
        # to go and research the city, which is wasted effort — and on a
        # provider-metered key, expensive wasted effort — when the truth is
        # that nothing could be read. Only an empty index is a 404.
        if outages:
            raise HTTPException(
                status_code=503,
                detail="Neither evidence store could be read, so this is an "
                "outage rather than an empty index — researching the city "
                "again will not help until it is fixed. "
                + " ".join(outages)
                + _shared_dependency_hint(outages),
            )
        raise HTTPException(
            status_code=404,
            detail=f"Nothing indexed for '{city}' yet — run /research first.",
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
