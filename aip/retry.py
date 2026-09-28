"""Retry with exponential backoff and full jitter for provider calls.

Shared by aip.llm and aip.embed so a chat call and an embedding call treat a
rate limit or a timeout the same way. Added for the RegTech project (see
CHANGELOG_REGTECH.md): embeddings previously had no retry, and one transient
timeout aborted a whole corpus indexing run.
"""
from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from aip import tracing
from aip.config import settings

T = TypeVar("T")

_RETRYABLE_MARKERS = (
    "ratelimit", "timeout", "overloaded", "apiconnection", "internalserver",
    "serviceunavailable", "529", "503", "502", "500", "429",
)


def is_retryable(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return any(m in name or m in text for m in _RETRYABLE_MARKERS)


def call_with_retries(fn: Callable[[], T], *, event: str, max_retries: int | None = None) -> T:
    """Call `fn()`; on a retryable error back off and try again, up to `max_retries` attempts."""
    attempts = max_retries or settings.max_retries
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not is_retryable(exc) or attempt == attempts - 1:
                raise
            sleep_s = min(30.0, (2 ** attempt)) * random.random()
            tracing.event(event, attempt=attempt + 1, sleep_s=round(sleep_s, 2), error=type(exc).__name__)
            time.sleep(sleep_s)
    raise RuntimeError("unreachable")  # pragma: no cover
