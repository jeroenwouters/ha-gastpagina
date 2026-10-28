"""Throttling of wrong codes: per IP, and globally."""

from __future__ import annotations

from collections import deque
import math
from typing import Literal

from homeassistant.util import dt as dt_util

from .const import ATTEMPT_WINDOW, GLOBAL_FAILURE_LIMIT, GLOBAL_FAILURE_WINDOW, GLOBAL_LOCK


class LoginLimiter:
    """At most `max_attempts` wrong codes per IP per 15 minutes.

    On top of that, `GLOBAL_FAILURE_LIMIT` wrong codes in an hour (from any
    address) closes login for everyone for a while.
    """

    def __init__(self, max_attempts: int) -> None:
        """Start with a clean slate."""
        self.max_attempts = max_attempts
        self._per_ip: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()
        self._locked_until = 0.0

    @staticmethod
    def _now() -> float:
        return dt_util.utcnow().timestamp()

    def _trim(self, now: float) -> None:
        ip_cutoff = now - ATTEMPT_WINDOW.total_seconds()
        for ip, failures in list(self._per_ip.items()):
            while failures and failures[0] <= ip_cutoff:
                failures.popleft()
            if not failures:
                del self._per_ip[ip]
        cutoff = now - GLOBAL_FAILURE_WINDOW.total_seconds()
        while self._all and self._all[0] <= cutoff:
            self._all.popleft()

    def retry_after(self, ip: str) -> int | None:
        """Seconds to wait before this IP may try again, or None."""
        now = self._now()
        self._trim(now)
        waits: list[float] = []
        if self._locked_until > now:
            waits.append(self._locked_until - now)
        failures = self._per_ip.get(ip)
        if failures and len(failures) >= self.max_attempts:
            waits.append(failures[0] + ATTEMPT_WINDOW.total_seconds() - now)
        return max(1, math.ceil(max(waits))) if waits else None

    def record_failure(self, ip: str) -> Literal["ip", "global"] | None:
        """Count a wrong code; says whether it just caused a block."""
        now = self._now()
        self._trim(now)
        failures = self._per_ip.setdefault(ip, deque())
        failures.append(now)
        self._all.append(now)
        if len(self._all) >= GLOBAL_FAILURE_LIMIT and self._locked_until <= now:
            self._locked_until = now + GLOBAL_LOCK.total_seconds()
            return "global"
        if len(failures) == self.max_attempts:
            return "ip"
        return None

    def record_success(self, ip: str) -> None:
        """A right code clears that address's count."""
        self._per_ip.pop(ip, None)

    def failures_last_hour(self) -> int:
        """Wrong codes in the past hour, all addresses."""
        self._trim(self._now())
        return len(self._all)
