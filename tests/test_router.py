from app.core.config import PoolEntry, RuleConfig, RuleWhen
from app.routing.pools import PoolResolver
from app.routing.router import Resolution, Router
from app.routing.rules import RuleEngine

POOLS = {
    "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
    "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
    "large-context": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
}
RULES = [
    RuleConfig(name="explicit-hint", when=RuleWhen(header="x-pool"),
               pool="{{ header.x-pool }}"),
    RuleConfig(name="has-tools", when=RuleWhen(has_tools=True), pool="default"),
    RuleConfig(name="long-context", when=RuleWhen(min_input_tokens=20000),
               pool="large-context"),
    RuleConfig(name="short-and-simple",
               when=RuleWhen(max_input_tokens=500, has_tools=False), pool="cheap"),
]


def _router():
    return Router(RuleEngine(RULES), PoolResolver(POOLS, "default"))


def test_short_request_routes_to_cheap_via_rule():
    r = _router().route({"messages": [{"role": "user", "content": "hi"}]}, {})
    assert r == Resolution(pool="cheap", provider="deepseek", model="deepseek-chat",
                           route_stage="rule", route_reason="rule:short-and-simple")


def test_header_hint_wins():
    r = _router().route({"messages": [{"role": "user", "content": "hi"}]},
                        {"x-pool": "cheap"})
    assert r.pool == "cheap" and r.route_stage == "rule"
    assert r.route_reason == "rule:explicit-hint"


def test_unknown_hint_degrades_to_default():
    r = _router().route({"messages": [{"role": "user", "content": "hi"}]},
                        {"x-pool": "bogus"})
    assert r.pool == "default" and r.provider == "anthropic"
    assert r.route_stage == "default"
    assert "unknown-pool" in r.route_reason and "bogus" in r.route_reason


def test_no_match_routes_to_default():
    long_text = "x" * 40000       # ~10000 tokens: above 500, below 20000 → no rule
    r = _router().route({"messages": [{"role": "user", "content": long_text}]}, {})
    assert r.pool == "default"
    assert r.route_stage == "default" and r.route_reason == "no-rule-matched"


def test_empty_rules_all_default():
    r = Router(RuleEngine([]), PoolResolver(POOLS, "default")).route(
        {"messages": []}, {})
    assert r.pool == "default" and r.route_reason == "no-rule-matched"
