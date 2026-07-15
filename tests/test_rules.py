from app.core.config import RuleConfig, RuleWhen
from app.routing.features import RequestFeatures
from app.routing.rules import RuleEngine, RuleMatch


def _features(tokens=100, tools=False, system="", headers=None):
    return RequestFeatures(tokens, tools, system, headers or {})


HINT = RuleConfig(name="explicit-hint", when=RuleWhen(header="x-pool"),
                  pool="{{ header.x-pool }}")
TOOLS = RuleConfig(name="has-tools", when=RuleWhen(has_tools=True), pool="default")
LONG = RuleConfig(name="long-context", when=RuleWhen(min_input_tokens=20000),
                  pool="large-context")
SHORT = RuleConfig(name="short-and-simple",
                   when=RuleWhen(max_input_tokens=500, has_tools=False), pool="cheap")
RULES = [HINT, TOOLS, LONG, SHORT]


def test_first_match_wins_short():
    m = RuleEngine(RULES).match(_features(tokens=100))
    assert m == RuleMatch(pool="cheap", rule_name="short-and-simple")


def test_header_hint_beats_later_rules():
    m = RuleEngine(RULES).match(_features(tokens=100, headers={"x-pool": "default"}))
    assert m == RuleMatch(pool="default", rule_name="explicit-hint")


def test_tools_route_to_default_even_when_short():
    m = RuleEngine(RULES).match(_features(tokens=100, tools=True))
    assert m == RuleMatch(pool="default", rule_name="has-tools")


def test_long_context_min_threshold():
    m = RuleEngine(RULES).match(_features(tokens=25000))
    assert m == RuleMatch(pool="large-context", rule_name="long-context")


def test_no_rule_matches_returns_none():
    # medium length, no tools, no header: matches none of the threshold rules
    assert RuleEngine(RULES).match(_features(tokens=5000)) is None


def test_template_header_absent_skips_rule():
    # explicit-hint's when(header) fails when header missing, so it never fires;
    # even a templated pool never yields an empty pool name.
    m = RuleEngine([HINT, SHORT]).match(_features(tokens=100))
    assert m == RuleMatch(pool="cheap", rule_name="short-and-simple")


def test_system_regex_match():
    rule = RuleConfig(name="translate", when=RuleWhen(system_regex="(?i)translate"),
                      pool="cheap")
    engine = RuleEngine([rule])
    assert engine.match(_features(system="Please Translate this")).pool == "cheap"
    assert engine.match(_features(system="summarize this")) is None


def test_empty_when_is_catch_all():
    rule = RuleConfig(name="catch", when=RuleWhen(), pool="default")
    assert RuleEngine([rule]).match(_features(tokens=99999)).pool == "default"
