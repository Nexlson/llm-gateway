from __future__ import annotations

from app.core.config import PriceEntry


class CostTracker:
    def __init__(self, prices: dict[str, PriceEntry]) -> None:
        self._prices = prices

    def compute(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price = self._prices.get(model)
        if price is None:
            return 0.0
        return (
            input_tokens / 1_000_000 * price.input_per_1m
            + output_tokens / 1_000_000 * price.output_per_1m
        )
