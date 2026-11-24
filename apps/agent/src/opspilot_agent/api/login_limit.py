"""Failed-login throttling for the shared demo accounts.

Two sliding windows: per client IP (stops one guesser) and per username (caps a distributed
guess against one account). Counts are in process memory, which fits the single Agent replica
the staging setup runs; use Redis or the gateway for more replicas. Behind Caddy the client IP
comes from X-Forwarded-For, so uvicorn must trust the proxy (`FORWARDED_ALLOW_IPS`).
"""

import math
import time
from collections import defaultdict, deque
from collections.abc import Callable


class LoginLimiter:
    def __init__(
        self,
        *,
        per_ip: int = 5,
        per_user: int = 20,
        window_s: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = {"ip": per_ip, "user": per_user}
        self.window_s = window_s
        self.clock = clock
        self._failures: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._last_sweep = clock()

    def _recent(self, key: tuple[str, str], now: float) -> deque[float]:
        q = self._failures[key]
        while q and q[0] <= now - self.window_s:
            q.popleft()
        if not q:
            self._failures.pop(key, None)  # keep memory bounded to active keys
            return deque()
        return q

    def retry_after(self, ip: str, username: str | None = None) -> int | None:
        """Seconds until a new attempt is allowed, or None if allowed now."""
        now = self.clock()
        waits = []
        for kind, value in self._keys(ip, username):
            q = self._recent((kind, value), now)
            if len(q) >= self.limits[kind]:
                waits.append(q[0] + self.window_s - now)
        return math.ceil(max(waits)) if waits else None

    def _sweep(self, now: float) -> None:
        """Drop keys whose failures all expired, so one-off IPs do not accumulate forever."""
        if now - self._last_sweep < self.window_s:
            return
        self._last_sweep = now
        for key in list(self._failures):
            self._recent(key, now)

    @staticmethod
    def _keys(ip: str, username: str | None) -> list[tuple[str, str]]:
        return [("ip", ip)] + ([("user", username)] if username is not None else [])

    def failed(self, ip: str, username: str | None = None) -> None:
        now = self.clock()
        self._sweep(now)
        for key in self._keys(ip, username):
            self._failures[key].append(now)

    def succeeded(self, ip: str) -> None:
        self._failures.pop(("ip", ip), None)
