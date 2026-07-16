from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.routing.features import RequestFeatures

# A ClassifierCaller runs the probe request and returns the model's raw text
# output. It MUST raise on any failure so the Classifier can degrade cleanly.
ClassifierCaller = Callable[[dict], Awaitable[str]]


@dataclass
class ClassifierDecision:
    pool: str
    reason: str


class Classifier:
    """Stage-2 router: a cheap model returns a pool label for requests the
    stage-1 rules did not resolve. Consulted only on no-match. Never raises;
    any timeout/error/garbage degrades to `fallback_pool`."""

    def __init__(
        self,
        caller: ClassifierCaller,
        labels: list[str],
        fallback_pool: str,
        timeout_s: float = 5.0,
        max_probe_chars: int = 2000,
        max_output_tokens: int = 8,
    ) -> None:
        self._caller = caller
        self._labels = labels
        self._fallback_pool = fallback_pool
        self._timeout_s = timeout_s
        self._max_probe_chars = max_probe_chars
        self._max_output_tokens = max_output_tokens

    async def classify(self, features: RequestFeatures) -> ClassifierDecision:
        probe = self._build_probe(features)
        try:
            raw = await asyncio.wait_for(self._caller(probe), self._timeout_s)
        except asyncio.TimeoutError:
            return self._degrade("timeout")
        except Exception:
            return self._degrade("error")

        label = self._parse(raw)
        if label is None:
            return self._degrade("garbage")
        return ClassifierDecision(pool=label, reason=f"classifier:label={label}")

    def _degrade(self, kind: str) -> ClassifierDecision:
        return ClassifierDecision(
            pool=self._fallback_pool,
            reason=f"classifier:{kind}->{self._fallback_pool}",
        )

    def _parse(self, raw: object) -> str | None:
        if not isinstance(raw, str):
            return None
        candidate = raw.strip()
        return candidate if candidate in self._labels else None

    def _build_probe(self, features: RequestFeatures) -> dict:
        system_slice = features.system_prompt[: self._max_probe_chars]
        user_slice = features.last_user_text[: self._max_probe_chars]
        labels = ", ".join(self._labels)
        instruction = (
            "You are a routing classifier. Choose the single best label for the "
            "request below. Reply with EXACTLY one of these labels and nothing "
            f"else: {labels}."
        )
        view = ""
        if system_slice:
            view += f"System prompt:\n{system_slice}\n\n"
        view += f"User message:\n{user_slice}"
        return {
            "model": "__classifier__",   # rewritten per pool entry by the adapter
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": view},
            ],
            "max_tokens": self._max_output_tokens,
            "temperature": 0,
        }
