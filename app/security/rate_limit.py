from __future__ import annotations

from collections import defaultdict, deque
from time import time

from app.core.errors import RateLimitError


class RateLimiter:
    def __init__(self):
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: int) -> None:
        now = time()
        queue = self._events[key]
        while queue and now - queue[0] > window_seconds:
            queue.popleft()
        if len(queue) >= limit:
            raise RateLimitError("Too many requests. Please wait and try again.")
        queue.append(now)

