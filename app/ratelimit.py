"""In-process sliding-window limits on posted messages.

The backend runs as one replica, so process memory is the whole picture. The
per-client limit is keyed on the client IP (uvicorn resolves it from
X-Forwarded-For behind the proxy); the global limit caps total Groq spend even
when the per-client key is spoofed.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Callable

from fastapi import HTTPException, Request

WINDOW_SECONDS = 60.0
LIMIT_MESSAGE = "Too many questions in a short time. Please wait a minute and try again."


class RateLimiter:
    def __init__(
        self,
        per_client: int,
        global_limit: int,
        window: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_client = per_client
        self.global_limit = global_limit
        self.window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._clients: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def hit(self, key: str) -> float | None:
        """Record one request. Returns seconds to wait when over a limit, else None."""
        now = self._clock()
        with self._lock:
            self._expire(self._all, now)
            hits = self._clients.get(key)
            if hits is not None:
                self._expire(hits, now)
            waits = []
            if self.global_limit > 0 and len(self._all) >= self.global_limit:
                waits.append(self._all[0] + self.window - now)
            if self.per_client > 0 and hits is not None and len(hits) >= self.per_client:
                waits.append(hits[0] + self.window - now)
            if waits:
                return max(waits)
            if self.global_limit > 0:
                self._all.append(now)
            if self.per_client > 0:
                self._clients.setdefault(key, deque()).append(now)
            self._prune(now)
            return None

    def _expire(self, hits: deque[float], now: float) -> None:
        while hits and hits[0] <= now - self.window:
            hits.popleft()

    def _prune(self, now: float) -> None:
        if len(self._clients) < 10_000:
            return
        for key in [k for k, v in self._clients.items() if not v or v[-1] <= now - self.window]:
            del self._clients[key]


def enforce_message_limit(request: Request) -> None:
    limiter: RateLimiter | None = getattr(request.app.state, "message_limiter", None)
    if limiter is None:
        return
    key = request.client.host if request.client else "unknown"
    wait = limiter.hit(key)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=LIMIT_MESSAGE,
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
