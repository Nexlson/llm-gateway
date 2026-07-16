import pytest

from app.core.config import PoolEntry
from app.providers.base import ProviderError, ProviderResponse
from app.routing.classifier_caller import ExecutorProbeCaller
from app.routing.executor import ExecutionOutcome

POOLS_FIRST = {"cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")]}


class FakePoolResolver:
    def first_entry(self, name):
        return POOLS_FIRST[name][0]


class FakeExecutor:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []

    async def execute(self, body, resolution):
        self.calls.append((body, resolution))
        return self._outcome


def _outcome(response=None, error=None):
    return ExecutionOutcome(provider="deepseek", model="deepseek-chat",
                            response=response, error=error,
                            fallback_from=None, fallback_hops=0)


async def test_extracts_assistant_text_and_uses_configured_pool():
    resp = ProviderResponse(200, {"choices": [{"message": {"content": "cheap"}}]}, 1, 1)
    ex = FakeExecutor(_outcome(response=resp))
    caller = ExecutorProbeCaller(ex, FakePoolResolver(), "cheap")
    out = await caller({"model": "__classifier__", "messages": []})
    assert out == "cheap"
    assert ex.calls[0][1].pool == "cheap"          # Resolution targeted the probe pool


async def test_raises_when_pool_exhausted():
    err = ProviderError("down", 503, "deepseek", retryable=True)
    ex = FakeExecutor(_outcome(response=None, error=err))
    caller = ExecutorProbeCaller(ex, FakePoolResolver(), "cheap")
    with pytest.raises(RuntimeError):
        await caller({"messages": []})


async def test_raises_on_malformed_body():
    resp = ProviderResponse(200, {"choices": []}, 1, 1)   # no message
    ex = FakeExecutor(_outcome(response=resp))
    caller = ExecutorProbeCaller(ex, FakePoolResolver(), "cheap")
    with pytest.raises((KeyError, IndexError, TypeError)):
        await caller({"messages": []})
