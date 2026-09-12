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
    # Defaults to Mistral Small on the free Experiment plan, and the reason is
    # the *shape* of the limit rather than its size.
    #
    # One city costs 150-250 model calls: roughly 80 of ours (one per source
    # extracted, one per claim adjudicated, one planner call per pass) plus
    # Graphiti's own entity extraction, which runs several calls per fact and
    # is usually the larger half. That is 400-600k tokens per run. Free tiers
    # meter this two different ways, and only one of them is survivable:
    #
    #   Per day      you hit a wall and wait 24h.
    #                gemini-3.x preview 20 RPD  -> cannot finish one pass
    #                gemini-2.5-flash  250 RPD  -> about one city
    #                gemini-2.5-flash-lite 1000 -> about four
    #                groq llama-3.1-8b: 14.4k RPD but only 6k TPM, and a
    #                single extraction call carrying a page body exceeds a
    #                whole minute's token budget on its own
    #   Per second   you go slower. Mistral Experiment: ~1 RPS, 500k TPM,
    #                1B tokens/month -> roughly 2000 runs a month.
    #
    # A rate limit we can wait out beats a ceiling we run out of, because this
    # is a batch job that already takes minutes. The cost is latency: ~250
    # serialized calls is 4-5 minutes of model time. See LLM_MAX_CONCURRENCY
    # and GRAPHITI_SEMAPHORE_LIMIT, which exist to respect that 1 RPS.
    LLM_API_KEY: str = _env(
        "LLM_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"
    )
    LLM_MODEL: str = _env("LLM_MODEL", default="mistral-small-latest")
    LLM_BASE_URL: str = _env("LLM_BASE_URL", default="https://api.mistral.ai/v1")
    # Graphiti uses a cheaper model for some internal extraction passes.
    LLM_SMALL_MODEL: str = _env("LLM_SMALL_MODEL", default=LLM_MODEL)

    # Empty by default, because the default provider does not reason and
    # several OpenAI-compatible shims reject parameters they do not implement —
    # which would fail every call rather than degrade.
    #
    # Set it to "low" on a model that thinks by default (Gemini 3, o-series).
    # There, thinking tokens are billed against the same output budget as the
    # answer, so a long extraction prompt left unbounded can spend the entire
    # budget reasoning and return an empty string. The symptom is not an error
    # but a run where every claim silently fails to parse.
    LLM_REASONING_EFFORT: str = _env("LLM_REASONING_EFFORT", default="")
    LLM_MAX_TOKENS: int = _int_env("LLM_MAX_TOKENS", 4096)

    # Graphiti's own budget, separate and much larger. Its entity-extraction
    # prompts are long, it asks for bigger JSON than we do, and it has no
    # reasoning_effort knob — so on a thinking model, headroom is the only
    # defence against an empty response. Too low here shows up as a graph
    # with no edges rather than as an error.
    GRAPHITI_MAX_TOKENS: int = _int_env("GRAPHITI_MAX_TOKENS", 16384)

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
    # Extraction and fact-checking are per-item and independent, so they run on
    # a thread pool. This must not exceed the provider's requests-per-second
    # allowance: the default provider permits ~1 RPS, so eight workers would
    # make seven of every eight calls a 429. The client does retry with
    # backoff, but then the run is spent waiting rather than working, and
    # bursts of retries are what a rate limiter punishes hardest.
    #
    # Raise it for a provider metered per day rather than per second — 8 is a
    # reasonable value on Gemini, and roughly 8x faster.
    LLM_MAX_CONCURRENCY: int = _int_env("LLM_MAX_CONCURRENCY", 1)

    # Graphiti's internal concurrency, which our thread pool does not govern:
    # it runs its own entity and edge extraction per episode and reads this
    # from the SEMAPHORE_LIMIT environment variable, defaulting to 20. Since
    # Graphiti is the single largest consumer of calls in a run, leaving that
    # at 20 against a 1 RPS provider produces a retry storm in the one place
    # least able to absorb it. Exported in app/stores/graph_store.py, which
    # must happen before graphiti_core is imported because it reads the
    # variable once at module scope.
    GRAPHITI_SEMAPHORE_LIMIT: int = _int_env("GRAPHITI_SEMAPHORE_LIMIT", 1)
    HTTP_TIMEOUT_SECONDS: int = _int_env("HTTP_TIMEOUT_SECONDS", 12)

    @property
    def is_mock(self) -> bool:
        return self.RUN_MODE == "MOCK"


settings = Settings()
