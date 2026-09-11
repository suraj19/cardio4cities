"""
Local embeddings, shared by the vector store and the knowledge graph.

Deliberate decision: embeddings are generated on-box with
sentence-transformers rather than through an API. Two reasons.

1. It decouples the graph from the chat provider. Graphiti embeds every
   node and edge it writes, and several good chat providers (DeepSeek,
   Groq) expose no embeddings endpoint at all — pointing Graphiti's
   OpenAIEmbedder at them returns 404 on every write. Embedding locally
   means the provider only has to serve chat completions, so DeepSeek,
   Gemini, Groq and OpenAI are interchangeable via two env vars.
2. Milvus and Graphiti then share one loaded model instead of two,
   which matters on a small demo VM.

The model is loaded lazily on first use, so importing the app does not
pull ~90MB off disk or block startup.
"""
from __future__ import annotations

import asyncio
from functools import lru_cache

from app.config import settings


@lru_cache(maxsize=1)
def get_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(settings.EMBEDDING_MODEL)


def embedding_dim() -> int:
    return get_model().get_sentence_embedding_dimension()


def embed_one(text: str) -> list[float]:
    return get_model().encode(text, convert_to_numpy=True).tolist()


def embed_many(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    return get_model().encode(texts, convert_to_numpy=True).tolist()


async def aembed_one(text: str) -> list[float]:
    """Async wrapper — encoding is CPU-bound and synchronous, so it runs in
    a worker thread to avoid stalling the event loop during a graph write."""
    return await asyncio.to_thread(embed_one, text)


async def aembed_many(texts: list[str]) -> list[list[float]]:
    return await asyncio.to_thread(embed_many, texts)
