"""
Central configuration. Every agent/store reads from here rather than
touching os.environ directly, so MOCK vs LIVE is a single switch.

The LLM is configured by endpoint rather than by vendor: anything that
speaks the OpenAI chat-completions protocol works (Mistral by default,
or Gemini, DeepSeek, Groq, OpenRouter, OpenAI, a local Ollama). Embeddings are
generated locally by sentence-transformers, so the provider only has to serve
chat completions — see app/llm/embeddings.py for why that matters.

Provider choice is a throughput decision, not a quality one. See LLM_MODEL
below: four of the six call sites want strict JSON rather than prose, and a
single city costs 150-250 calls.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# SQLite and Milvus Lite both write here, and neither creates the directory
# itself — without this the first run dies on "unable to open database file".
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _env(name: str, *fallbacks: str, default: str = "") -> str:
    """First non-empty value among `name` and any legacy fallback names."""
    for key in (name, *fallbacks):
        value = os.getenv(key)
        if value:
            return value
    return default


def _int_env(name: str, default: int) -> int:
    """An integer setting, tolerant of a blank or junk value in `.env`.

    `int(os.getenv(name, "8"))` looks equivalent and is not: os.getenv only
    supplies the default when the variable is *absent*, so `MAX_SOURCES=` in
    a .env file yields int("") and a ValueError at import time. That is an
    unhelpful way to fail, because commenting a line out and blanking it are
    the two obvious ways to say "use the default" and only one of them worked
    — and the traceback points at config.py rather than at the edit.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


class Settings:
    RUN_MODE: str = os.getenv("RUN_MODE", "LIVE").upper()  # MOCK | LIVE

    # INFO shows each pipeline node starting, its duration and what it
    # produced, which is the difference between watching a slow run and
    # wondering whether it has hung. DEBUG adds the HTTP libraries' own noise.
    LOG_LEVEL: str = _env("LOG_LEVEL", default="INFO").strip().upper()

    # --- LLM: any OpenAI-compatible chat endpoint ---------------------
    # Defaults to Mistral Large 3 for judgement and Mistral Small 4 for
    # volume. Two models rather than one, because this workload is not one
    # workload — see LLM_BULK_MODEL below, which is where nearly all the calls
    # and nearly all the money go.
    #
    # One city costs 150-250 model calls: roughly 80 of ours (one per source
    # extracted, one per batch of claims adjudicated, one planner call per
    # pass) plus Graphiti's own entity extraction, which runs several calls
    # per fact and is reliably the larger half. That is 400-600k tokens per
    # run, and the provider choice is decided by HOW a free tier meters that,
    # not by how generous it sounds:
    #
    #   Per day      a wall. The run stops unfinished and waits 24h.
    #                gemini-3.5-flash   20 req/day  -> cannot finish one pass
    #                groq llama-3.3-70b 100k tok/day -> dies about a fifth in
    #   Per month    a budget. 400-600k against Mistral's 1B/month is noise.
    #   Per second   a throttle. The run takes longer and still finishes,
    #                which is the only one of the three a batch job absorbs.
    #
    # Mistral is the default because it is the only free tier that finishes a
    # run: metered per request-rate against a monthly token budget of 1B, so
    # nothing in it can stop a city mid-pass. The throttle is the cost, and
    # POST /research no longer holds an HTTP request open (see app/jobs.py),
    # so a slow run is merely slow rather than impossible.
    #
    # Check your own rate in Admin Panel > API > Limits: Mistral does not
    # publish the free number and reports of it vary by two orders of
    # magnitude. It sets how long a run takes, not whether it completes.
    LLM_API_KEY: str = _env(
        "LLM_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"
    )
    LLM_BASE_URL: str = _env("LLM_BASE_URL", default="https://api.mistral.ai/v1")

    # The judgement model: query planning, fact-check adjudication, the report
    # narrative and /ask. Perhaps 30 calls per run, and every one of them is a
    # decision a reader would notice getting wrong.
    #
    # Medium 3.5 rather than Large 3 because Large is NOT served on the free
    # tier: it is absent from GET /v1/models for a free key and returns
    # 403 'tier_not_allowed' (code 1910) if you ask for it anyway. Medium is
    # listed for the same key, so it is the best judgement model a no-bill
    # account can reach.
    #
    # ON A PAID KEY, SWITCH THIS TO mistral-large-latest. Large 3 is both
    # stronger and ~3x cheaper than Medium 3.5 — $0.50/$1.50 per 1M tokens
    # against $1.50/$7.50 — so the free-tier default is the expensive one if
    # you ever start paying. Mistral's generation numbers do not order the
    # way the names imply, which is why this is worth checking rather than
    # assuming.
    LLM_MODEL: str = _env("LLM_MODEL", default="mistral-medium-latest")

    # The workhorse: claim extraction and Graphiti's entity extraction. Perhaps
    # 120 of the run's 150-250 calls, and not one of them is a judgement — they
    # turn prose into JSON against a fixed schema. Paying judgement rates for
    # that is the single most expensive mistake available on this pipeline:
    # Small 4 is $0.15/$0.60 per 1M tokens against Large 3's $0.50/$1.50, so
    # routing the volume here is roughly a 3x saving on a run whose output
    # quality is indistinguishable.
    #
    # Small 4 is a 119B mixture-of-experts model with native JSON output, so
    # it clears the size threshold below which structured extraction starts
    # inventing fields — which is the failure that makes a cheaper bulk model
    # a false economy.
    #
    # LLM_SMALL_MODEL is honoured as the older name for this setting, since it
    # is what Graphiti's own config calls the same idea.
    LLM_BULK_MODEL: str = _env(
        "LLM_BULK_MODEL", "LLM_SMALL_MODEL", default="mistral-small-latest"
    )

    # Thinking effort, which is a cost control rather than a quality dial:
    # thinking tokens are billed at the OUTPUT rate and counted against the
    # same budget as the answer.
    #
    # The two defaults differ because the two models differ, not because the
    # roles do. Mistral Small 4 is a hybrid instruct/reasoning model and
    # implements the parameter; Mistral Large 3 does not, and an
    # OpenAI-compatible shim REJECTS a parameter it does not implement rather
    # than ignoring it — so a value here would fail every judgement call with
    # an opaque 400. Empty means "send no such field", which is the only safe
    # default for a model that has no opinion about it.
    #
    # "none" is a value Mistral defines, not an absence: it means answer
    # directly with no thinking trace, which is exactly right for filling a
    # fixed schema. Do NOT set "high" here without reading app/llm/client.py
    # first — on Small 4 it makes `message.content` a list of chunks instead
    # of a string, and every caller in this codebase parses a string.
    #
    # `scripts/check_services.py` calls both profiles and names the effort it
    # sent, so a rejected value shows up in preflight rather than as an empty
    # graph three minutes into a run.
    LLM_REASONING_EFFORT: str = _env("LLM_REASONING_EFFORT", default="")
    LLM_BULK_REASONING_EFFORT: str = _env(
        "LLM_BULK_REASONING_EFFORT", default="none"
    )

    # Output ceilings. These bound thinking tokens too, so they cannot be cut
    # to the size of the expected answer: a model that exhausts the budget
    # mid-thought returns an empty string, and every caller here reads that as
    # "nothing found" rather than as an error. Both values are sized for the
    # JSON actually asked for plus room to think, not for the JSON alone.
    LLM_MAX_TOKENS: int = _int_env("LLM_MAX_TOKENS", 4096)
    LLM_BULK_MAX_TOKENS: int = _int_env("LLM_BULK_MAX_TOKENS", 2048)

    # Graphiti's own budget, separate and much larger. Its entity-extraction
    # prompts are long, it asks for bigger JSON than we do, and it has no
    # reasoning_effort knob — so on a thinking model, headroom is the only
    # defence against an empty response. Too low here shows up as a graph
    # with no edges rather than as an error.
    GRAPHITI_MAX_TOKENS: int = _int_env("GRAPHITI_MAX_TOKENS", 16384)

    # Which model Graphiti runs its own extraction on. Defaults to the bulk
    # model deliberately: Graphiti is the largest LLM consumer in a run by a
    # wide margin, and what it does with an episode — name the entities, name
    # the relationship between them — is extraction, not judgement. Pointing
    # this at the judgement model is what makes a run cost dollars.
    GRAPHITI_MODEL: str = _env("GRAPHITI_MODEL", default=LLM_BULK_MODEL)

    # --- Embeddings: local, no API key, shared by Milvus and Graphiti --
    EMBEDDING_MODEL: str = _env("EMBEDDING_MODEL", default="all-MiniLM-L6-v2")

    # --- Search ---
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

    # --- Official statistics APIs -------------------------------------
    # WHO GHO and the World Bank serve cv_burden and health_system from
    # maintained time series instead of scraped prose. Neither needs a key.
    # They are country-level, so every claim they produce is flagged as
    # national context rather than city evidence — see
    # app/agents/official_data_agent.py. Turn off to demo the web-only path.
    ENABLE_OFFICIAL_DATA: bool = _env(
        "ENABLE_OFFICIAL_DATA", default="true"
    ).strip().lower() not in ("0", "false", "no", "off")

    # --- Relational ---
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/cardio4cities.db")

    # --- Vector ---
    # A path ending in .db (e.g. ./data/milvus.db) runs Milvus Lite embedded —
    # no server needed, which is what the demo deployment uses. Point at
    # http://host:19530 or a Zilliz Cloud endpoint for a real Milvus cluster.
    MILVUS_URI: str = os.getenv("MILVUS_URI", "./data/milvus.db")
    MILVUS_TOKEN: str = os.getenv("MILVUS_TOKEN", "")
    MILVUS_COLLECTION: str = os.getenv("MILVUS_COLLECTION", "city_passages")

    # --- Graph: Neo4j Graph Database Sandbox ---
    NEO4J_URI: str = os.getenv("NEO4J_URI", "")
    NEO4J_USER: str = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD: str = os.getenv("NEO4J_PASSWORD", "")

    # --- Research policy ---
    MAX_PLANNER_RETRIES: int = _int_env("MAX_PLANNER_RETRIES", 2)
    MIN_SOURCES_FOR_VERIFIED: int = _int_env("MIN_SOURCES_FOR_VERIFIED", 2)
    # Coverage is judged across dimensions, not in total: a run that found ten
    # facts about programmes and nothing about policy has not understood the
    # city. Below this many covered dimensions the planner re-plans, targeting
    # only the dimensions that came back empty.
    MIN_DIMENSIONS_COVERED: int = _int_env("MIN_DIMENSIONS_COVERED", 3)

    # --- Cost and latency budget --------------------------------------
    # Every one of these bounds a multiplier. Unbounded, a five-dimension run
    # fans out to ~50 candidate URLs, ~50 extraction calls and ~100 fact-check
    # calls each carrying every other passage as context — minutes of latency
    # and a context window blown on the first city. These caps are the main
    # "what did you cut when time was limited" answer in the report.
    QUERIES_PER_DIMENSION: int = _int_env("QUERIES_PER_DIMENSION", 2)
    MAX_SOURCES_PER_DIMENSION: int = _int_env("MAX_SOURCES_PER_DIMENSION", 4)
    MAX_CLAIMS_PER_PASSAGE: int = _int_env("MAX_CLAIMS_PER_PASSAGE", 3)
    # How many rival passages the fact-checker sees per claim, and how much of
    # each. Corroboration needs the most relevant few, not all of them.
    FACT_CHECK_CONTEXT_PASSAGES: int = _int_env("FACT_CHECK_CONTEXT_PASSAGES", 4)
    FACT_CHECK_CONTEXT_CHARS: int = _int_env("FACT_CHECK_CONTEXT_CHARS", 1500)
    # How many claims share one adjudication call. Fact-checking dominates the
    # token bill — each claim used to re-send the same few passages and the
    # same system prompt, about 12x more text than the run actually contained.
    # Only claims from the same origin domain are ever grouped, so they are
    # judged against an identical candidate pool either way.
    #
    # Raise it to cut tokens and wall-clock further; lower it to 1 to restore
    # strictly one call per claim, which is the most conservative setting if a
    # model starts confusing claims with each other.
    FACT_CHECK_BATCH_SIZE: int = _int_env("FACT_CHECK_BATCH_SIZE", 5)
    PASSAGE_CHAR_LIMIT: int = _int_env("PASSAGE_CHAR_LIMIT", 5000)

    # How much of a passage the claim extractor is shown. Distinct from
    # PASSAGE_CHAR_LIMIT, which is how much is kept — the full text is still
    # stored and embedded for /ask, and only this window is sent to the model.
    #
    # Separating the two is the largest single input-token saving available
    # here. Extraction is one call per source and the prompt is the page, so a
    # 5000-character page body was the run's biggest single payload, most of
    # it irrelevant to the dimension the source was found for. The window is
    # chosen by overlap with the dimension brief rather than taken from the
    # top of the page (see extraction_agent._focused_window), so halving the
    # characters does not halve the signal — a figure two thirds of the way
    # down a programme page was previously truncated away at full price.
    EXTRACTION_CHAR_LIMIT: int = _int_env("EXTRACTION_CHAR_LIMIT", 2500)

    # Extraction and fact-checking are per-item and independent, so they run on
    # a thread pool. This must not exceed the provider's request-rate
    # allowance: Mistral's free plan throttles hard, so eight workers would
    # make seven of every eight calls a 429. The client does retry with
    # backoff, but then the run is spent waiting rather than working, and
    # bursts of retries are what a rate limiter punishes hardest.
    #
    # 1 is the honest default for a throttled free key. Raise it to 4-8 on a
    # paid key or a provider metered per day, for roughly that much more
    # speed — read your actual rate off the provider's limits page first.
    LLM_MAX_CONCURRENCY: int = _int_env("LLM_MAX_CONCURRENCY", 1)

    # Concurrency for work that is pure network wait and costs no tokens:
    # robots.txt, the X-Robots-Tag HEAD request, and fetching page bodies.
    # Separate from LLM_MAX_CONCURRENCY because they are limited by different
    # things and sharing one number meant the provider's rate limit silently
    # throttled the crawler. At LLM_MAX_CONCURRENCY=1 — the correct value for
    # a per-second-metered provider — a twenty-source pass made roughly forty
    # HTTP requests strictly one at a time, each with a 12s timeout. That was
    # minutes of the run spent waiting on sockets for no reason.
    HTTP_MAX_CONCURRENCY: int = _int_env("HTTP_MAX_CONCURRENCY", 8)

    # Parallel search queries. Bounded well below HTTP_MAX_CONCURRENCY on
    # purpose: `ddgs` scrapes consumer engines, and a pass already issues ten
    # queries, so firing them all at once is the reliable way to earn the
    # Ratelimit error that shows up as an empty brief. Tavily tolerates more.
    SEARCH_MAX_CONCURRENCY: int = _int_env("SEARCH_MAX_CONCURRENCY", 3)

    # Parallel Graphiti episode writes. Defaults to 1, which is not timidity:
    # `add_episode` resolves each extracted entity against what is already in
    # the graph, so two episodes naming the same organization concurrently can
    # both decide it is new and create it twice. Raise it to shorten the run's
    # longest node, accepting some duplicate entities; the facts and their
    # provenance are unaffected either way, since those live in the relational
    # audit trail.
    GRAPH_WRITE_CONCURRENCY: int = _int_env("GRAPH_WRITE_CONCURRENCY", 1)

    # Graphiti's internal concurrency, which our thread pool does not govern:
    # it runs its own entity and edge extraction per episode and reads this
    # from the SEMAPHORE_LIMIT environment variable, defaulting to 20. Since
    # Graphiti is the single largest consumer of calls in a run, leaving that
    # at 20 against a per-second-metered provider produces a retry storm in
    # the one place least able to absorb it. Exported in
    # app/stores/graph_store.py, which must happen before graphiti_core is
    # imported because it reads the variable once at module scope.
    #
    # 1 matches LLM_MAX_CONCURRENCY above and the throttled free key it is
    # sized for. Raise both together on a paid key; raising only this one is
    # the more common mistake, because Graphiti makes more calls than we do.
    GRAPHITI_SEMAPHORE_LIMIT: int = _int_env("GRAPHITI_SEMAPHORE_LIMIT", 1)
    HTTP_TIMEOUT_SECONDS: int = _int_env("HTTP_TIMEOUT_SECONDS", 12)

    # --- Research jobs ------------------------------------------------
    # A research run is minutes long and POST /research no longer waits for
    # it — the request registers a job and returns an id to poll. See
    # app/jobs.py for why the HTTP request could not stay in front of it.
    #
    # Concurrency is capped because the relational store is SQLite by default
    # and SQLite takes one writer at a time; two simultaneous runs would spend
    # their time contending for the write lock, not researching. Raise it with
    # DATABASE_URL pointed at Postgres.
    MAX_CONCURRENT_RESEARCH: int = _int_env("MAX_CONCURRENT_RESEARCH", 2)
    # How long a finished job's result stays pollable. Long enough that a
    # browser left on another tab still finds its brief; the report itself is
    # in the relational store regardless, so expiry costs the progress detail
    # and not the deliverable.
    JOB_RETENTION_SECONDS: int = _int_env("JOB_RETENTION_SECONDS", 3600)

    # Load the embedding model at startup instead of on the first request.
    # ~60s of model load otherwise lands on whoever asks first, which during a
    # demo is indistinguishable from the app having hung — and on a platform
    # with an HTTP deadline it can consume the entire request budget.
    PREWARM_EMBEDDINGS: bool = _env(
        "PREWARM_EMBEDDINGS", default="true"
    ).strip().lower() not in ("0", "false", "no", "off")

    @property
    def is_mock(self) -> bool:
        return self.RUN_MODE == "MOCK"


settings = Settings()
