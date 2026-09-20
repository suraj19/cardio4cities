"""
Central configuration. Every agent/store reads from here rather than
touching os.environ directly, so MOCK vs LIVE is a single switch.

The LLM is configured by endpoint rather than by vendor: anything that
speaks the OpenAI chat-completions protocol works (Mistral by default,
or Gemini, DeepSeek, Groq, OpenRouter, OpenAI, a local Ollama). Embeddings are
generated locally by fastembed/ONNX, so the provider only has to serve
chat completions — see app/llm/embeddings.py for why that matters.

Provider choice is a throughput decision, not a quality one. See LLM_MODEL
below: four of the six call sites want strict JSON rather than prose, and a
single city costs 150-250 calls.
"""
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

# Hosts that mean "a model server on this machine". Ollama, LM Studio and
# llama.cpp all ignore the Authorization header entirely, so requiring an API
# key for them turns a correct configuration into a startup error telling the
# operator to go and obtain a key that does not exist.
_LOCAL_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
)


def _is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(".local")

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


def _csv_env(name: str, default: str = "") -> tuple[str, ...]:
    """A comma-separated setting as a lowercased tuple, blanks discarded."""
    return tuple(
        item.strip().lower().lstrip(".")
        for item in _env(name, default=default).split(",")
        if item.strip()
    )


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
    #                gemini-3.5-flash    20 req/day  -> cannot finish one pass
    #                groq openai/gpt-oss 200k tok/day -> about half a city, and
    #                its 8k TPM throttles the half it does allow to ~50 min
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
    # Small 4 here — the same model the bulk profile uses — because it is the
    # only OPEN-WEIGHT model Mistral serves: Apache 2.0, weights published, so
    # the whole pipeline can be inspected, self-hosted or audited by whoever
    # reads the brief. Medium and Large are both stronger and both closed.
    #
    # The judgement/bulk distinction is NOT lost by sharing a model. It moves
    # to the thinking budget below: high for the ~30 decisions a reader would
    # notice getting wrong, none for the ~120 calls that fill a fixed schema.
    # One model, two efforts.
    #
    # ON A PAID KEY, mistral-large-latest is the upgrade, and it is also ~3x
    # CHEAPER than Medium 3.5 — $0.50/$1.50 per 1M tokens against $1.50/$7.50.
    # Mistral's generation numbers do not order the way the names imply, which
    # is why this is worth checking rather than assuming. Large is absent from
    # GET /v1/models for a free key and returns 403 'tier_not_allowed' (1910),
    # and it does NOT implement reasoning_effort — so switching to it means
    # blanking LLM_REASONING_EFFORT in the same change.
    LLM_MODEL: str = _env("LLM_MODEL", default="mistral-small-latest")

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
    # Now that both profiles run Mistral Small 4, these two values are what
    # separates a judgement call from a bulk one. Small 4 is a hybrid
    # instruct/reasoning model and implements only two settings: "high" and
    # "none". "none" is a value Mistral defines rather than an absence — answer
    # directly, no thinking trace — which is exactly right for filling a fixed
    # schema, and it is where most of the token saving on a run comes from.
    #
    # "high" is safe here, though it was not always: on Small 4 it returns
    # `message.content` as a list of thinking/text chunks instead of a string,
    # and every caller in this codebase parses a string. `_answer_text` in
    # app/llm/client.py unwraps that and drops the trace. If you remove it,
    # this default has to go back to empty.
    #
    # Blank means "send no such field", which is the only safe value for a
    # model that does not implement the parameter: an OpenAI-compatible shim
    # REJECTS a parameter it does not know rather than ignoring it, so a value
    # aimed at the wrong model fails every call with an opaque 400.
    #
    # `scripts/check_services.py` calls both profiles and names the effort it
    # sent, so a rejected value shows up in preflight rather than as an empty
    # graph three minutes into a run.
    #
    # Both default to blank against a LOCAL endpoint, the same way MILVUS_URI
    # alone decides Lite-or-cluster. Ollama does accept the field, but on a
    # local server the trade the split encodes no longer exists: thinking is
    # billed in wall-clock rather than money, and an 8B model asked to
    # deliberate spends minutes per call to reach the same JSON. LM Studio and
    # llama.cpp are also reached this way and do not all implement it. Set
    # either explicitly to override.
    LLM_REASONING_EFFORT: str = _env(
        "LLM_REASONING_EFFORT",
        default="" if _is_local_url(LLM_BASE_URL) else "high",
    )
    LLM_BULK_REASONING_EFFORT: str = _env(
        "LLM_BULK_REASONING_EFFORT",
        default="" if _is_local_url(LLM_BASE_URL) else "none",
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

    # --- Source domain policy -----------------------------------------
    # Applied in search_agent, *before* the per-dimension cap above, so a
    # result that was never going to be usable cannot first consume one of the
    # few source slots a dimension is allowed.
    #
    # The denylist is an editorial rule rather than a performance one. A
    # user-generated video or a social post is not citable evidence in a
    # public-health brief no matter how relevant a search engine finds it, so
    # fetching one only spends a model call to discover it says nothing.
    SOURCE_DOMAIN_DENYLIST: tuple[str, ...] = _csv_env(
        "SOURCE_DOMAIN_DENYLIST",
        default=(
            "youtube.com,youtu.be,facebook.com,instagram.com,twitter.com,"
            "x.com,tiktok.com,pinterest.com,reddit.com,quora.com,"
            "linkedin.com,tripadvisor.com,amazon.com"
        ),
    )
    # Empty by default, meaning discovery stays open — most of the value of
    # the brief is in sources nobody thought to nominate, and a curated list
    # cannot surprise you. Set it to pin a demo to known-good domains, which
    # is faster and repeatable; the run then *discloses* the restriction in
    # the report, because gaps produced by a narrowed search would otherwise
    # read as an absence of published information.
    SOURCE_DOMAIN_ALLOWLIST: tuple[str, ...] = _csv_env("SOURCE_DOMAIN_ALLOWLIST")
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

    # Load the embedding model at startup instead of on the first request, so
    # the ~60s model load does not land on whoever asks first — during a demo
    # that is indistinguishable from the app having hung.
    #
    # OFF by default, because the cost of being wrong is asymmetric. Turning it
    # on allocates the embedding model in the first seconds of every container
    # start, and on a memory-capped host that spike can be an OOM kill
    # rather than an exception: the prewarm helper catches everything it can,
    # but a process killed by the kernel cannot log. Deferring the load costs
    # one slow request; doing it at boot can cost the deployment. Turn it on
    # once the host is known to have headroom (see docs/DEPLOYMENT.md).
    PREWARM_EMBEDDINGS: bool = _env(
        "PREWARM_EMBEDDINGS", default="false"
    ).strip().lower() not in ("0", "false", "no", "off")

    # --- Observability -------------------------------------------------
    # OFF by default, for the same reason PREWARM_EMBEDDINGS is: this has to
    # keep fitting a 0.5 GB container, and an exporter that cannot reach its
    # collector should never be something a research run waits on. Turning it
    # on with no OpenTelemetry installed logs once and continues without it.
    #
    # Everything below is deliberately thin. The endpoint and headers are
    # configured with the SDK's OWN standard variables
    # (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS) wherever
    # possible, so anyone who has configured an OTel SDK before already knows
    # how to point this one; OTEL_EXPORTER_ENDPOINT here is only an override.
    OTEL_ENABLED: bool = _env("OTEL_ENABLED", default="false").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )
    OTEL_SERVICE_NAME: str = _env("OTEL_SERVICE_NAME", default="cardio4cities")
    OTEL_SERVICE_VERSION: str = _env("OTEL_SERVICE_VERSION", default="0.1.0")
    OTEL_EXPORTER_ENDPOINT: str = _env(
        "OTEL_EXPORTER_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    # Prints spans to stdout instead of shipping them. The difference between
    # telemetry you can try in one command and telemetry you have to deploy a
    # collector for.
    OTEL_CONSOLE_EXPORT: bool = _env(
        "OTEL_CONSOLE_EXPORT", default="false"
    ).strip().lower() not in ("0", "false", "no", "off", "")
    OTEL_METRICS_ENABLED: bool = _env(
        "OTEL_METRICS_ENABLED", default="true"
    ).strip().lower() not in ("0", "false", "no", "off", "")
    # Escape hatch for a backend that takes traces and metrics on different
    # endpoints — and the override that re-enables metrics when the shared
    # endpoint is a traces-only one like Opik's.
    OTEL_METRICS_ENDPOINT: str = _env(
        "OTEL_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"
    )
    # Record prompts and completions on LLM spans. OFF by default, and the
    # default is the careful one on purpose: prompts here contain scraped
    # third-party page content, and a trace backend is a copy of it that
    # nobody audits.
    #
    # Turn it on for an LLM-native dashboard — Opik, Langfuse, LangSmith —
    # where the prompt/completion pair IS the product. With it off, those
    # tools still draw the correct trace tree with timings and token counts,
    # but every span detail panel is empty, which reads as a broken
    # integration rather than a deliberate one.
    OTEL_CAPTURE_CONTENT: bool = _env(
        "OTEL_CAPTURE_CONTENT", default="false"
    ).strip().lower() not in ("0", "false", "no", "off", "")
    # Ceiling per recorded field, since a page body can be tens of KB and
    # every backend drops or truncates oversized attributes anyway.
    OTEL_CAPTURE_CONTENT_CHARS: int = int(
        _env("OTEL_CAPTURE_CONTENT_CHARS", default="4000")
    )

    @property
    def is_mock(self) -> bool:
        return self.RUN_MODE == "MOCK"

    @property
    def llm_is_local(self) -> bool:
        """Whether LLM_BASE_URL points at a model server on this machine.

        Ollama, LM Studio and llama.cpp ignore the Authorization header
        entirely — Ollama's own documentation describes the api_key as
        "required but ignored". Demanding LLM_API_KEY for them turns a correct
        configuration into a startup error telling the operator to obtain a key
        that does not exist, which is a worse outcome than the missing-key case
        the check was written for.

        Read as a property rather than frozen at import so a test that patches
        LLM_BASE_URL sees the change.
        """
        return _is_local_url(self.LLM_BASE_URL)


settings = Settings()
