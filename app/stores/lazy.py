"""
Lazy store proxy.

All three stores are constructed on first use rather than at import, and for
the same reason: building one opens a connection (Neo4j, Milvus, the SQLite
engine), and doing that at import time meant an unreachable dependency
stopped `app.main` from importing at all — which takes `/health` down with
it and leaves the container in a restart loop. Deferred, an unreachable
dependency instead costs exactly the feature that needs it, and the
persistence node downgrades it to a warning on the run.

Each store previously carried its own proxy class with hand-written
forwarding methods, which meant adding a method to a store required adding
it to the proxy too — and forgetting was silent, because the proxy simply
would not expose it. Forwarding through `__getattr__` makes the proxy
transparent instead, and works for async methods unchanged: the attribute
lookup returns the bound coroutine function and the caller awaits it.
"""
from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")


class LazyStore:
    def __init__(self, factory: Callable[[], T]):
        # Assigned through the instance dict, so normal attribute lookup
        # resolves both of these and __getattr__ is never consulted for them.
        self._factory = factory
        self._impl: T | None = None

    def _get(self) -> T:
        if self._impl is None:
            self._impl = self._factory()
        return self._impl

    def __getattr__(self, name: str):
        return getattr(self._get(), name)
