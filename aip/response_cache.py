"""[regtech] Response-level caches for a service (Lab 7 Part B1).

`aip.cache` caches *model calls*: a repeated question still pays for embedding the query, retrieval and
validation, and a different wording of the same question misses. These caches sit in front of the whole
pipeline and return the finished response:

    exact    = ExactCache()                          # key: normalised question (+ scope)
    semantic = SemanticCache(threshold=0.97)          # key: question embedding, cosine >= threshold

    hit = exact.get(q, scope) or semantic.get(q, scope)

`scope` partitions entries that must never be shared: a different mode, corpus version or date. A
semantic cache does not fail loudly when its threshold is too low; it answers a *different question*.
Measure the threshold on labelled question pairs before switching it on (regtech: Stage 7 study).
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from aip.guards import normalise_text


def exact_key(question: str, scope: str = "") -> str:
    return hashlib.sha256(f"{scope}\x00{normalise_text(question)}".encode("utf-8")).hexdigest()


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        n = self.hits + self.misses
        return self.hits / n if n else 0.0

    def as_dict(self) -> dict[str, float]:
        return {"hits": self.hits, "misses": self.misses, "hit_rate": round(self.hit_rate, 3)}


class ExactCache:
    """LRU map from (scope, normalised question) to a response."""

    def __init__(self, max_items: int = 1000):
        self.max_items = max_items
        self._items: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        self.stats = CacheStats()

    def get(self, question: str, scope: str = "") -> Any | None:
        k = exact_key(question, scope)
        with self._lock:
            if k in self._items:
                self._items.move_to_end(k)
                self.stats.hits += 1
                return self._items[k]
            self.stats.misses += 1
            return None

    def put(self, question: str, value: Any, scope: str = "") -> None:
        with self._lock:
            self._items[exact_key(question, scope)] = value
            self._items.move_to_end(exact_key(question, scope))
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class SemanticHit:
    value: Any
    similarity: float
    matched_question: str


@dataclass
class _Entry:
    question: str
    vector: np.ndarray
    value: Any = field(repr=False)


class SemanticCache:
    """Nearest stored question by cosine similarity; a hit only at or above `threshold`.

    `threshold=None` disables lookups (entries are still stored, so it can be switched on later).
    Vectors come from aip.embed (query side), which is itself cached and cost-accounted."""

    def __init__(self, threshold: float | None = 0.97, max_items: int = 1000, embed_fn=None):
        self.threshold, self.max_items = threshold, max_items
        self._embed = embed_fn
        self._entries: dict[str, list[_Entry]] = {}
        self._lock = threading.Lock()
        self.stats = CacheStats()

    def _vector(self, question: str) -> np.ndarray:
        if self._embed is None:
            from aip.embed import embed_batch
            self._embed = lambda texts: embed_batch(texts, input_type="query")
        v = np.asarray(self._embed([question])[0], dtype=np.float32)
        return v / (np.linalg.norm(v) or 1.0)

    def nearest(self, question: str, scope: str = "") -> SemanticHit | None:
        """The most similar stored question in this scope, whatever the threshold (for studies/debugging)."""
        entries = self._entries.get(scope) or []
        if not entries:
            return None
        v = self._vector(question)
        sims = np.stack([e.vector for e in entries]) @ v
        i = int(np.argmax(sims))
        return SemanticHit(entries[i].value, float(sims[i]), entries[i].question)

    def get(self, question: str, scope: str = "") -> SemanticHit | None:
        if self.threshold is None:
            return None
        best = self.nearest(question, scope)
        with self._lock:
            if best and best.similarity >= self.threshold:
                self.stats.hits += 1
                return best
            self.stats.misses += 1
            return None

    def put(self, question: str, value: Any, scope: str = "") -> None:
        entry = _Entry(question, self._vector(question), value)
        with self._lock:
            bucket = self._entries.setdefault(scope, [])
            bucket.append(entry)
            if len(bucket) > self.max_items:
                bucket.pop(0)

    def __len__(self) -> int:
        return sum(len(v) for v in self._entries.values())
