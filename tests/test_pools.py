from app.core.config import PoolEntry
from app.routing.pools import PoolResolver

POOLS = {
    "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat"),
              PoolEntry(provider="anthropic", model="claude-haiku-4-5")],
    "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
}


def test_has_pool():
    r = PoolResolver(POOLS, "default")
    assert r.has_pool("cheap") is True
    assert r.has_pool("nope") is False
    assert r.default_pool == "default"


def test_entries_preserve_order():
    entries = PoolResolver(POOLS, "default").entries("cheap")
    assert [(e.provider, e.model) for e in entries] == [
        ("deepseek", "deepseek-chat"), ("anthropic", "claude-haiku-4-5")]


def test_first_entry():
    e = PoolResolver(POOLS, "default").first_entry("cheap")
    assert (e.provider, e.model) == ("deepseek", "deepseek-chat")
