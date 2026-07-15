from app.core.config import PoolEntry
from app.routing.pools import PoolResolver

POOLS = {
    "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat"),
              PoolEntry(provider="anthropic", model="claude-haiku-4-5")],
    "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
}


def test_resolves_known_pool_to_first_entry():
    r = PoolResolver(POOLS, "default").resolve("cheap")
    assert (r.pool, r.provider, r.model) == ("cheap", "deepseek", "deepseek-chat")
    assert r.route_stage == "passthrough"
    assert r.route_reason == "m1-passthrough:pool=cheap"


def test_unknown_model_falls_to_default_pool():
    r = PoolResolver(POOLS, "default").resolve("gpt-4o")
    assert (r.pool, r.provider, r.model) == ("default", "anthropic", "claude-sonnet-5")
    assert "model-not-a-pool" in r.route_reason
