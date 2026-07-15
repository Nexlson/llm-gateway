from pathlib import Path

import pytest

from app.core.config import load_config, AppConfig

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"

# Env vars the committed example references. Set them for every load test.
EXAMPLE_ENV = {
    "GATEWAY_API_KEY": "test-gateway-key",
    "DEEPSEEK_API_KEY": "test-deepseek-key",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
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
