import asyncio

from app.routing.classifier import Classifier, ClassifierDecision
from app.routing.features import RequestFeatures

LABELS = ["cheap", "default"]


def _features(system="", last_user=""):
    return RequestFeatures(input_tokens=0, has_tools=False, system_prompt=system,
                           headers={}, last_user_text=last_user)


def _classifier(caller, timeout_s=5.0, max_probe_chars=2000, max_output_tokens=8):
    return Classifier(caller=caller, labels=LABELS, fallback_pool="default",
                      timeout_s=timeout_s, max_probe_chars=max_probe_chars,
                      max_output_tokens=max_output_tokens)


async def test_valid_label_returns_that_pool():
    async def caller(probe):
        return "default"
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d == ClassifierDecision(pool="default", reason="classifier:label=default")


async def test_label_is_stripped():
    async def caller(probe):
        return "  cheap\n"
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d == ClassifierDecision(pool="cheap", reason="classifier:label=cheap")


async def test_timeout_degrades_to_fallback():
    async def caller(probe):
        raise asyncio.TimeoutError
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d == ClassifierDecision(pool="default", reason="classifier:timeout->default")


async def test_error_degrades_to_fallback():
    async def caller(probe):
        raise RuntimeError("boom")
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d == ClassifierDecision(pool="default", reason="classifier:error->default")


async def test_garbage_label_degrades_to_fallback():
    async def caller(probe):
        return "banana"
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d == ClassifierDecision(pool="default", reason="classifier:garbage->default")


async def test_empty_output_degrades_to_fallback():
    async def caller(probe):
        return ""
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d.pool == "default" and d.reason == "classifier:garbage->default"


async def test_non_string_output_degrades_to_fallback():
    async def caller(probe):
        return None
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d.pool == "default" and d.reason == "classifier:garbage->default"


async def test_hard_timeout_bounds_slow_caller():
    async def caller(probe):
        await asyncio.sleep(5)
        return "cheap"
    loop = asyncio.get_event_loop()
    start = loop.time()
    d = await _classifier(caller, timeout_s=0.05).classify(_features(last_user="hi"))
    elapsed = loop.time() - start
    assert d.reason == "classifier:timeout->default"
    assert elapsed < 1.0            # wait_for cancelled the slow call promptly


async def test_probe_is_truncated_and_constrained():
    captured = {}

    async def caller(probe):
        captured["probe"] = probe
        return "default"

    big_sys = "S" * 5000
    big_user = "U" * 5000
    await _classifier(caller, max_probe_chars=2000, max_output_tokens=8).classify(
        _features(system=big_sys, last_user=big_user))

    probe = captured["probe"]
    roles = [m["role"] for m in probe["messages"]]
    assert roles == ["system", "user"]                      # exactly one of each
    assert probe["max_tokens"] == 8                          # constrained output
    user_content = probe["messages"][1]["content"]
    # each source slice is truncated to exactly max_probe_chars (2000) chars;
    # count via the longest run so the fixed label prefixes don't pollute it.
    assert "S" * 2000 in user_content and "S" * 2001 not in user_content
    assert "U" * 2000 in user_content and "U" * 2001 not in user_content


async def test_only_last_user_message_informs_decision():
    # earlier turns are excluded: only system + last user text reach the probe
    captured = {}

    async def caller(probe):
        captured["probe"] = probe
        return "cheap"

    await _classifier(caller).classify(_features(system="sys", last_user="the-last-ask"))
    content = captured["probe"]["messages"][1]["content"]
    assert "the-last-ask" in content and "sys" in content
