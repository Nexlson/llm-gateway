from app.core.config import PoolEntry
from app.providers.base import ProviderError, ProviderResponse
from app.routing.executor import ExecutionOutcome, FallbackExecutor
from app.routing.health import HealthTracker
from app.routing.pools import PoolResolver
from app.routing.router import Resolution

POOLS = {
    "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat"),
              PoolEntry(provider="anthropic", model="claude-haiku-4-5")],
    "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
}


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t


class FakeAdapter:
    def __init__(self, name, response=None, error=None):
        self.name = name
        self._response = response
        self._error = error
        self.calls = 0

    async def chat_completion(self, payload, model):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._response


def _resolution(pool="cheap"):
    entry = POOLS[pool][0]
    return Resolution(pool=pool, provider=entry.provider, model=entry.model,
                      route_stage="rule", route_reason="rule:short")


def _executor(adapters, clock=None, cooldown=60):
    health = HealthTracker(cooldown_seconds=cooldown,
                           clock=(clock.now if clock else None) or (lambda: 0.0))
    return FallbackExecutor(adapters, PoolResolver(POOLS, "default"), health), health


async def test_first_entry_success_no_fallback():
    resp = ProviderResponse(200, {"id": "ok"}, 5, 2)
    ds = FakeAdapter("deepseek", response=resp)
    an = FakeAdapter("anthropic")
    ex, _ = _executor({"deepseek": ds, "anthropic": an})
    out = await ex.execute({"model": "m"}, _resolution())
    assert out.response is resp and out.error is None
    assert (out.provider, out.model) == ("deepseek", "deepseek-chat")
    assert out.fallback_from is None and out.fallback_hops == 0
    assert ds.calls == 1 and an.calls == 0


async def test_retryable_advances_to_second_entry():
    err = ProviderError("down", 503, "deepseek", retryable=True)
    resp = ProviderResponse(200, {"id": "ok"}, 5, 2)
    ds = FakeAdapter("deepseek", error=err)
    an = FakeAdapter("anthropic", response=resp)
    ex, health = _executor({"deepseek": ds, "anthropic": an})
    out = await ex.execute({"model": "m"}, _resolution())
    assert out.response is resp
    assert (out.provider, out.model) == ("anthropic", "claude-haiku-4-5")
    assert out.fallback_from == "deepseek/deepseek-chat"
    assert out.fallback_hops == 1
    assert ds.calls == 1 and an.calls == 1
    assert health.is_healthy("deepseek", "deepseek-chat") is False   # marked


async def test_non_retryable_returns_immediately_and_does_not_mark():
    err = ProviderError("bad", 400, "deepseek", retryable=False,
                        error_type="invalid_request_error")
    ds = FakeAdapter("deepseek", error=err)
    an = FakeAdapter("anthropic")
    ex, health = _executor({"deepseek": ds, "anthropic": an})
    out = await ex.execute({"model": "m"}, _resolution())
    assert out.response is None and out.error is err
    assert (out.provider, out.model) == ("deepseek", "deepseek-chat")
    assert out.fallback_from is None and out.fallback_hops == 0
    assert ds.calls == 1 and an.calls == 0                 # chain not burned
    assert health.is_healthy("deepseek", "deepseek-chat") is True    # NOT marked


async def test_exhaustion_returns_last_error():
    e1 = ProviderError("down", 503, "deepseek", retryable=True)
    e2 = ProviderError("busy", 429, "anthropic", retryable=True)
    ds = FakeAdapter("deepseek", error=e1)
    an = FakeAdapter("anthropic", error=e2)
    ex, _ = _executor({"deepseek": ds, "anthropic": an})
    out = await ex.execute({"model": "m"}, _resolution())
    assert out.response is None and out.error is e2       # last error
    assert (out.provider, out.model) == ("anthropic", "claude-haiku-4-5")
    assert out.fallback_from == "deepseek/deepseek-chat"
    assert out.fallback_hops == 1


async def test_cooldown_skips_unhealthy_entry_on_next_request():
    e1 = ProviderError("down", 503, "deepseek", retryable=True)
    resp = ProviderResponse(200, {"id": "ok"}, 1, 1)
    clock = FakeClock(1000.0)
    ds = FakeAdapter("deepseek", error=e1)
    an = FakeAdapter("anthropic", response=resp)
    ex, _ = _executor({"deepseek": ds, "anthropic": an}, clock=clock)

    first = await ex.execute({"model": "m"}, _resolution())     # ds fails, an serves
    assert first.fallback_hops == 1 and ds.calls == 1

    second = await ex.execute({"model": "m"}, _resolution())    # ds cooling -> skipped
    assert second.response is resp
    assert (second.provider, second.model) == ("anthropic", "claude-haiku-4-5")
    assert second.fallback_from is None and second.fallback_hops == 0
    assert ds.calls == 1                                        # ds NOT called again


async def test_free_attempt_after_cooldown_elapses():
    e1 = ProviderError("down", 503, "deepseek", retryable=True)
    resp = ProviderResponse(200, {"id": "ok"}, 1, 1)
    clock = FakeClock(1000.0)
    ds = FakeAdapter("deepseek", error=e1)
    an = FakeAdapter("anthropic", response=resp)
    ex, _ = _executor({"deepseek": ds, "anthropic": an}, clock=clock)

    await ex.execute({"model": "m"}, _resolution())            # ds fails (call 1)
    clock.t = 1060.0                                           # cooldown elapsed
    await ex.execute({"model": "m"}, _resolution())           # ds free attempt (call 2)
    assert ds.calls == 2


async def test_whole_pool_cooling_best_effort():
    resp = ProviderResponse(200, {"id": "ok"}, 1, 1)
    clock = FakeClock(1000.0)
    # both entries succeed now, but both are pre-marked unhealthy
    ds = FakeAdapter("deepseek", response=resp)
    an = FakeAdapter("anthropic", response=resp)
    ex, health = _executor({"deepseek": ds, "anthropic": an}, clock=clock)
    health.mark_unhealthy("deepseek", "deepseek-chat")
    health.mark_unhealthy("anthropic", "claude-haiku-4-5")
    out = await ex.execute({"model": "m"}, _resolution())      # best-effort, not a no-op
    assert out.response is resp
    assert (out.provider, out.model) == ("deepseek", "deepseek-chat")
    assert ds.calls == 1
