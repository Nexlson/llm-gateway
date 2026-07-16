from pathlib import Path

import pytest

from app.core.config import load_config, AppConfig

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"

# Env vars the committed example references. Set them for every load test.
EXAMPLE_ENV = {
    "GATEWAY_API_KEY": "test-gateway-key",
    "DEEPSEEK_API_KEY": "test-deepseek-key",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "GROK_API_KEY": "test-grok-key",
    "OPEN_AI_API_KEY": "test-openai-key",
}


def _set_env(monkeypatch, env: dict[str, str]) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_loads_example_config(monkeypatch):
    _set_env(monkeypatch, EXAMPLE_ENV)
    cfg = load_config(EXAMPLE)
    assert isinstance(cfg, AppConfig)
    # api_key is resolved from os.environ[api_key_env], never stored in YAML.
    assert cfg.api_key == "test-gateway-key"
    assert cfg.providers["deepseek"].api_key == "test-deepseek-key"
    assert cfg.providers["grok"].api_key == "test-grok-key"
    assert cfg.providers["openai"].api_key == "test-openai-key"
    assert cfg.default_pool in cfg.pools
    assert cfg.pools["default"][0].provider in cfg.providers
    assert cfg.prices != {}  # M2 example ships a populated price table


def test_missing_referenced_env_var_fails_fast(monkeypatch):
    # Gateway key present, but a provider's referenced env var is unset -> boot fails.
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError):
        load_config(EXAMPLE)


def test_rejects_default_pool_not_in_pools(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "api_key_env: K\n"
        "default_pool: missing\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
    )
    with pytest.raises(ValueError):
        load_config(bad)


def test_rejects_pool_entry_unknown_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: nope, model: m}\n"
    )
    with pytest.raises(ValueError):
        load_config(bad)


def test_loads_rules_in_order(monkeypatch):
    _set_env(monkeypatch, EXAMPLE_ENV)
    cfg = load_config(EXAMPLE)
    names = [r.name for r in cfg.rules]
    assert names[0] == "explicit-hint"          # explicit hint evaluated first
    assert "short-and-simple" in names
    # prices are now populated (M2)
    assert cfg.prices != {}
    assert "deepseek-chat" in cfg.prices


def test_rejects_rule_literal_pool_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
        "rules:\n  - name: bad\n    when: {max_input_tokens: 10}\n    pool: nope\n"
    )
    with pytest.raises(ValueError):
        load_config(bad)


def test_rejects_rule_bad_regex(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
        "rules:\n  - name: bad\n    when: {system_regex: '('}\n    pool: default\n"
    )
    with pytest.raises(ValueError):
        load_config(bad)


def test_cooldown_seconds_defaults_to_60(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    ok.write_text(
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
    )
    cfg = load_config(ok)
    assert cfg.cooldown_seconds == 60.0


def test_cooldown_seconds_from_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    ok.write_text(
        "api_key_env: K\n"
        "cooldown_seconds: 15\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
    )
    cfg = load_config(ok)
    assert cfg.cooldown_seconds == 15.0


def test_example_config_ships_cooldown(monkeypatch):
    _set_env(monkeypatch, EXAMPLE_ENV)
    cfg = load_config(EXAMPLE)
    assert cfg.cooldown_seconds == 60.0


def _classifier_yaml(block: str) -> str:
    return (
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n"
        "  default:\n    - {provider: p, model: m}\n"
        "  cheap:\n    - {provider: p, model: c}\n"
        + block
    )


def test_classifier_defaults_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    ok.write_text(_classifier_yaml(""))
    cfg = load_config(ok)
    assert cfg.classifier.enabled is False


def test_example_config_ships_enabled_classifier(monkeypatch):
    _set_env(monkeypatch, EXAMPLE_ENV)
    cfg = load_config(EXAMPLE)
    assert cfg.classifier.enabled is True
    assert cfg.classifier.pool == "cheap"
    assert cfg.classifier.labels == ["cheap", "default"]
    assert cfg.classifier.fallback_pool == "default"


def test_enabled_classifier_rejects_unknown_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(_classifier_yaml(
        "classifier:\n  enabled: true\n  pool: nope\n"
        "  labels: [cheap, default]\n  fallback_pool: default\n"))
    with pytest.raises(ValueError):
        load_config(bad)


def test_enabled_classifier_rejects_unknown_fallback_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(_classifier_yaml(
        "classifier:\n  enabled: true\n  pool: cheap\n"
        "  labels: [cheap, default]\n  fallback_pool: nope\n"))
    with pytest.raises(ValueError):
        load_config(bad)


def test_enabled_classifier_rejects_unknown_label(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(_classifier_yaml(
        "classifier:\n  enabled: true\n  pool: cheap\n"
        "  labels: [cheap, nope]\n  fallback_pool: default\n"))
    with pytest.raises(ValueError):
        load_config(bad)


def test_enabled_classifier_rejects_empty_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(_classifier_yaml(
        "classifier:\n  enabled: true\n  pool: cheap\n"
        "  labels: []\n  fallback_pool: default\n"))
    with pytest.raises(ValueError):
        load_config(bad)


def test_disabled_classifier_skips_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    # references a bogus pool but disabled -> must NOT raise
    ok.write_text(_classifier_yaml(
        "classifier:\n  enabled: false\n  pool: nope\n"
        "  labels: [nope]\n  fallback_pool: nope\n"))
    cfg = load_config(ok)
    assert cfg.classifier.enabled is False


def test_allows_templated_pool_without_static_pool_check(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    ok.write_text(
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
        "rules:\n  - name: hint\n    when: {header: x-pool}\n    pool: '{{ header.x-pool }}'\n"
    )
    cfg = load_config(ok)             # must not raise despite templated pool
    assert cfg.rules[0].pool == "{{ header.x-pool }}"


def test_dashboard_config_defaults_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    ok.write_text(_classifier_yaml(""))
    cfg = load_config(ok)
    assert cfg.dashboard.budget_usd is None
    assert cfg.dashboard.baseline_model is None
    assert cfg.dashboard.recent_request_limit == 50
    assert cfg.dashboard.fallback_event_limit == 25


def test_example_config_ships_dashboard_settings(monkeypatch):
    _set_env(monkeypatch, EXAMPLE_ENV)
    cfg = load_config(EXAMPLE)
    assert cfg.dashboard.budget_usd is None
    assert cfg.dashboard.baseline_model == "claude-sonnet-5"


def test_dashboard_rejects_unknown_baseline_model(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        _classifier_yaml("")
        + "prices:\n  m: {input_per_1m: 1, output_per_1m: 2}\n"
        + "dashboard:\n  baseline_model: missing\n"
    )
    with pytest.raises(ValueError, match="dashboard.baseline_model"):
        load_config(bad)
