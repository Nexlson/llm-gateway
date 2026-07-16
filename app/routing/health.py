from __future__ import annotations

import time
from typing import Callable


class HealthTracker:
    """Plain monotonic-clock cooldown store keyed by (provider, model).

    A failing entry is marked unhealthy and skipped until `cooldown_seconds`
    have elapsed, after which it is healthy again (one free attempt). No
    half-open state, no rolling windows. Pure and clock-injectable so tests are
    deterministic without sleeping.
    """

    def __init__(
        self, cooldown_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._cooldown_until: dict[tuple[str, str], float] = {}

    def is_healthy(self, provider: str, model: str) -> bool:
        until = self._cooldown_until.get((provider, model))
        if until is None:
            return True
        return self._clock() >= until

    def mark_unhealthy(self, provider: str, model: str) -> None:
        self._cooldown_until[(provider, model)] = self._clock() + self._cooldown
