"""
Central configuration. Every agent/store reads from here rather than
touching os.environ directly, so MOCK vs LIVE is a single switch.

The LLM is configured by endpoint rather than by vendor: anything that
speaks the OpenAI chat-completions protocol works (Gemini by default,
or DeepSeek, Groq, OpenRouter, OpenAI, a local Ollama). Embeddings are generated
locally by sentence-transformers, so the provider only has to serve chat
completions — see app/llm/embeddings.py for why that matters.
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


class Settings:
    RUN_MODE: str = os.getenv("RUN_MODE", "LIVE").upper()  # MOCK | LIVE

    # --- LLM: any OpenAI-compatible chat endpoint ---------------------
    # Defaults to Gemini 3.5 Flash, which is the recommended setup: it has the
    # strongest JSON adherence of the cheap models, which is what Graphiti's
    # entity extraction depends on. Alternatives are listed in .env.example.
    LLM_API_KEY: str = _env("LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    LLM_MODEL: str = _env("LLM_MODEL", default="gemini-3.5-flash")
    LLM_BASE_URL: str = _env(
        "LLM_BASE_URL",
        default="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    # Graphiti uses a cheaper model for some internal extraction passes.
    LLM_SMALL_MODEL: str = _env("LLM_SMALL_MODEL", default=LLM_MODEL)

    # Gemini 3 models always think, and thinking tokens are billed against the
    # same output budget as the answer. Left unset, a long extraction prompt can
    # spend the whole budget reasoning and return an empty string. "low" keeps
    # the budget small; the generous max_tokens leaves room for the JSON itself.
    # Clear LLM_REASONING_EFFORT for providers that reject the parameter.
    LLM_REASONING_EFFORT: str = _env("LLM_REASONING_EFFORT", default="low")
    LLM_MAX_TOKENS: int = int(_env("LLM_MAX_TOKENS", default="4096"))

    # --- Embeddings: local, no API key, shared by Milvus and Graphiti --
    EMBEDDING_MODEL: str = _env("EMBEDDING_MODEL", default="all-MiniLM-L6-v2")

    # --- Search ---
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

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
    MAX_PLANNER_RETRIES: int = int(os.getenv("MAX_PLANNER_RETRIES", "2"))
    MIN_SOURCES_FOR_VERIFIED: int = int(os.getenv("MIN_SOURCES_FOR_VERIFIED", "2"))
    # Coverage is judged across dimensions, not in total: a run that found ten
    # facts about programmes and nothing about policy has not understood the
    # city. Below this many covered dimensions the planner re-plans, targeting
    # only the dimensions that came back empty.
    MIN_DIMENSIONS_COVERED: int = int(os.getenv("MIN_DIMENSIONS_COVERED", "3"))

    # --- Cost and latency budget --------------------------------------
    # Every one of these bounds a multiplier. Unbounded, a five-dimension run
    # fans out to ~50 candidate URLs, ~50 extraction calls and ~100 fact-check
    # calls each carrying every other passage as context — minutes of latency
    # and a context window blown on the first city. These caps are the main
    # "what did you cut when time was limited" answer in the report.
    QUERIES_PER_DIMENSION: int = int(os.getenv("QUERIES_PER_DIMENSION", "2"))
    MAX_SOURCES_PER_DIMENSION: int = int(os.getenv("MAX_SOURCES_PER_DIMENSION", "4"))
    MAX_CLAIMS_PER_PASSAGE: int = int(os.getenv("MAX_CLAIMS_PER_PASSAGE", "3"))
    # How many rival passages the fact-checker sees per claim, and how much of
    # each. Corroboration needs the most relevant few, not all of them.
    FACT_CHECK_CONTEXT_PASSAGES: int = int(os.getenv("FACT_CHECK_CONTEXT_PASSAGES", "4"))
    FACT_CHECK_CONTEXT_CHARS: int = int(os.getenv("FACT_CHECK_CONTEXT_CHARS", "1500"))
    PASSAGE_CHAR_LIMIT: int = int(os.getenv("PASSAGE_CHAR_LIMIT", "5000"))
    # Extraction and fact-checking are per-item and independent, so they run
    # on a thread pool. Keep this under the provider's rate limit.
    LLM_MAX_CONCURRENCY: int = int(os.getenv("LLM_MAX_CONCURRENCY", "8"))
    HTTP_TIMEOUT_SECONDS: int = int(os.getenv("HTTP_TIMEOUT_SECONDS", "12"))

    @property
    def is_mock(self) -> bool:
        return self.RUN_MODE == "MOCK"


settings = Settings()
