import pytest

from app.core.config import PoolEntry, RuleConfig, RuleWhen
from app.routing.classifier import ClassifierDecision
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


class FakeClassifier:
    """Records whether it was consulted and returns a fixed decision."""

    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    async def classify(self, features):
        self.calls += 1
        return self.decision


def _router(classifier=None):
    return Router(RuleEngine(RULES), PoolResolver(POOLS, "default"), classifier)


async def test_short_request_routes_to_cheap_via_rule():
    r = await _router().route({"messages": [{"role": "user", "content": "hi"}]}, {})
    assert r == Resolution(pool="cheap", provider="deepseek", model="deepseek-chat",
                           route_stage="rule", route_reason="rule:short-and-simple")


async def test_header_hint_wins():
    r = await _router().route({"messages": [{"role": "user", "content": "hi"}]},
                              {"x-pool": "cheap"})
    assert r.pool == "cheap" and r.route_stage == "rule"
    assert r.route_reason == "rule:explicit-hint"


async def test_unknown_hint_degrades_to_default():
    r = await _router().route({"messages": [{"role": "user", "content": "hi"}]},
                              {"x-pool": "bogus"})
    assert r.pool == "default" and r.provider == "anthropic"
    assert r.route_stage == "default"
    assert "unknown-pool" in r.route_reason and "bogus" in r.route_reason


async def test_no_match_routes_to_default_when_classifier_absent():
    long_text = "x" * 40000       # ~10000 tokens: above 500, below 20000 -> no rule
    r = await _router().route({"messages": [{"role": "user", "content": long_text}]}, {})
    assert r.pool == "default"
    assert r.route_stage == "default" and r.route_reason == "no-rule-matched"


async def test_empty_rules_all_default():
    r = await Router(RuleEngine([]), PoolResolver(POOLS, "default")).route(
        {"messages": []}, {})
    assert r.pool == "default" and r.route_reason == "no-rule-matched"


async def test_no_match_consults_classifier_when_present():
    clf = FakeClassifier(ClassifierDecision(pool="cheap", reason="classifier:label=cheap"))
    long_text = "x" * 40000       # no rule matches
    r = await _router(clf).route({"messages": [{"role": "user", "content": long_text}]}, {})
    assert clf.calls == 1
    assert r.pool == "cheap" and r.provider == "deepseek"
    assert r.route_stage == "classifier"
    assert r.route_reason == "classifier:label=cheap"


async def test_rule_match_never_consults_classifier():
    clf = FakeClassifier(ClassifierDecision(pool="cheap", reason="classifier:label=cheap"))
    # short, tool-less -> matches short-and-simple; classifier must NOT run
    r = await _router(clf).route({"messages": [{"role": "user", "content": "hi"}]}, {})
    assert clf.calls == 0
    assert r.route_stage == "rule" and r.route_reason == "rule:short-and-simple"


async def test_classifier_fallback_pool_decision_recorded():
    clf = FakeClassifier(ClassifierDecision(pool="default", reason="classifier:timeout->default"))
    long_text = "x" * 40000
    r = await _router(clf).route({"messages": [{"role": "user", "content": long_text}]}, {})
    assert r.pool == "default" and r.route_stage == "classifier"
    assert r.route_reason == "classifier:timeout->default"


async def test_classifier_unknown_pool_guards_to_default():
    clf = FakeClassifier(ClassifierDecision(pool="ghost", reason="classifier:label=ghost"))
    long_text = "x" * 40000
    r = await _router(clf).route({"messages": [{"role": "user", "content": long_text}]}, {})
    assert r.pool == "default" and r.route_stage == "default"
    assert "unknown-pool" in r.route_reason and "ghost" in r.route_reason
