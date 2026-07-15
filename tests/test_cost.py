from app.core.config import PriceEntry
from app.cost.tracker import CostTracker


def test_unknown_model_is_zero():
    assert CostTracker({}).compute("x", 1000, 1000) == 0.0


def test_known_model_cost():
    prices = {"deepseek-chat": PriceEntry(input_per_1m=0.27, output_per_1m=1.10)}
    cost = CostTracker(prices).compute("deepseek-chat", 1_000_000, 1_000_000)
    assert round(cost, 6) == round(0.27 + 1.10, 6)
