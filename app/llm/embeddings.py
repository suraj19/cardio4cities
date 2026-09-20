"""
Local embeddings, shared by the vector store and the knowledge graph.

Deliberate decision: embeddings are generated on-box rather than through an
API. Two reasons.

1. It decouples the graph from the chat provider. Graphiti embeds every
   node and edge it writes, and several good chat providers (DeepSeek,
   Groq) expose no embeddings endpoint at all — pointing Graphiti's
   OpenAIEmbedder at them returns 404 on every write. Embedding locally
   means the provider only has to serve chat completions, so DeepSeek,
   Mistral, Gemini, Groq and OpenAI are interchangeable via two env vars.
2. Milvus and Graphiti then share one loaded model instead of two,
   which matters on a small demo VM.

The backend is ONNX via fastembed rather than sentence-transformers. That is a
memory decision, not a quality one — the weights are the same
all-MiniLM-L6-v2. sentence-transformers depends on torch, and torch plus the
model is roughly 500-800MB resident and ~2GB of image once pip resolves the
default linux wheel. onnxruntime holds the same model in roughly 150-250MB
with no CUDA payload. On a 0.5GB container that is the difference between
serving and being OOM-killed, and this pipeline encodes short passages a batch
at a time rather than training anything, so there was nothing torch was
buying here.

Ranking is unaffected by the switch. Milvus queries the collection with the
COSINE metric and the graph's reranker computes cosine directly; both are
scale-invariant, so fastembed returning L2-normalised vectors where
sentence-transformers did not changes no ordering. The width is identical, so
an existing collection stays valid.

The model is loaded lazily on first use, so importing the app does not pull
the weights off disk or block startup.
"""
from __future__ import annotations

import asyncio
from functools import lru_cache

from app.config import settings
from app.telemetry import span


def _model_name() -> str:
    """The model id in the form fastembed expects.

    fastembed identifies models by their full Hugging Face repo id, while
    EMBEDDING_MODEL has always held the bare `all-MiniLM-L6-v2` — a value
    that is in every existing .env and in the deployed service's variables.
    Accepting the short form means this backend change does not require a
    coordinated configuration edit everywhere the app runs.
    """
    name = settings.EMBEDDING_MODEL.strip()
    return name if "/" in name else f"sentence-transformers/{name}"


@lru_cache(maxsize=1)
def get_model():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=_model_name())


@lru_cache(maxsize=1)
def embedding_dim() -> int:
    """The embedding width, read off the model rather than hard-coded.

    Probed with one short encode because fastembed has moved this between a
    class method and a model-description dict across releases, while the
    output width has not moved at all. The vector store builds its collection
    schema from this value, so guessing wrong is a dimension mismatch that
    only surfaces on a later insert.
    """
    return len(embed_one("dimension probe"))


def embed_one(text: str) -> list[float]:
    return embed_many([text])[0]


def embed_many(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    # Traced because this is the one component both evidence stores share.
    # Milvus embeds to search, and Graphiti's retrieval embeds through the
    # same model, so when it cannot load — a missing package, or no network
    # egress to Hugging Face on first use — `/ask` reports the vector store
    # and the knowledge graph as two separate outages and the shared cause is
    # invisible. As a span it is one failed child under both parents.
    with span(
        "embeddings.encode",
        **{
            "gen_ai.operation.name": "embeddings",
            "gen_ai.request.model": _model_name(),
            "cardio4cities.embeddings.count": len(texts),
        },
    ):
        return _encode(texts)


def _encode(texts: list[str]) -> list[list[float]]:
    # `parallel` is deliberately not set. Passing it makes fastembed fork
    # worker processes, each holding its own copy of the model, which is the
    # opposite of the reason this backend was chosen.
    return [vector.tolist() for vector in get_model().embed(list(texts))]


async def aembed_one(text: str) -> list[float]:
    """Async wrapper — encoding is CPU-bound and synchronous, so it runs in
    a worker thread to avoid stalling the event loop during a graph write."""
    return await asyncio.to_thread(embed_one, text)


async def aembed_many(texts: list[str]) -> list[list[float]]:
    return await asyncio.to_thread(embed_many, texts)
