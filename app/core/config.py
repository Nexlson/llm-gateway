from __future__ import annotations

import os
import re

import yaml
from pydantic import BaseModel, Field


class ProviderConfig(BaseModel):
    base_url: str
    api_key_env: str = ""     # name of the env var holding this provider's key
    api_key: str = ""         # resolved from os.environ[api_key_env] at load time
    timeout_s: float = 30.0


class PoolEntry(BaseModel):
    provider: str
    model: str


class PriceEntry(BaseModel):
    input_per_1m: float = 0.0
    output_per_1m: float = 0.0


class RuleWhen(BaseModel):
    max_input_tokens: int | None = None
    min_input_tokens: int | None = None
    has_tools: bool | None = None
    header: str | None = None
    system_regex: str | None = None


class RuleConfig(BaseModel):
    name: str
    when: "RuleWhen" = Field(default_factory=lambda: RuleWhen())
    pool: str


class ClassifierConfig(BaseModel):
    enabled: bool = False
    pool: str = ""              # cheap pool the classifier itself runs on
    labels: list[str] = Field(default_factory=list)  # candidate pool names
    fallback_pool: str = ""     # used on timeout / error / garbage
    timeout_s: float = 5.0      # hard timeout for the classifier call
    max_probe_chars: int = 2000  # per-slice truncation cap (system, last user)
    max_output_tokens: int = 8   # constrained classifier output


class AppConfig(BaseModel):
    api_key_env: str = ""     # name of the env var holding the gateway's static key
    api_key: str = ""         # resolved from os.environ[api_key_env] at load time
    db_path: str = "gateway.db"
    cooldown_seconds: float = 60.0
    default_pool: str
    providers: dict[str, ProviderConfig]
    pools: dict[str, list[PoolEntry]]
    prices: dict[str, PriceEntry] = Field(default_factory=dict)
    rules: list[RuleConfig] = Field(default_factory=list)
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)


def load_config(path: str | os.PathLike) -> AppConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = AppConfig(**raw)
    _resolve_secrets(cfg)
    _validate(cfg)
    return cfg


def _require_env(var: str) -> str:
    """Resolve a required secret from the environment; fail fast if unset/empty."""
    value = os.environ.get(var)
    if not value:
        raise ValueError(f"required environment variable '{var}' is unset or empty")
    return value


def _resolve_secrets(cfg: AppConfig) -> None:
    # No secret is stored in config.yaml — resolve each *_env name from os.environ.
    cfg.api_key = _require_env(cfg.api_key_env)
    for provider in cfg.providers.values():
        provider.api_key = _require_env(provider.api_key_env)


def _validate(cfg: AppConfig) -> None:
    if cfg.default_pool not in cfg.pools:
        raise ValueError(f"default_pool '{cfg.default_pool}' is not a defined pool")
    for pool_name, entries in cfg.pools.items():
        if not entries:
            raise ValueError(f"pool '{pool_name}' has no entries")
        for entry in entries:
            if entry.provider not in cfg.providers:
                raise ValueError(
                    f"pool '{pool_name}' references unknown provider '{entry.provider}'"
                )
    _TEMPLATE_ONLY = re.compile(r"^\s*\{\{\s*header\.[A-Za-z0-9_-]+\s*\}\}\s*$")
    for rule in cfg.rules:
        if rule.when.system_regex is not None:
            try:
                re.compile(rule.when.system_regex)
            except re.error as exc:
                raise ValueError(
                    f"rule '{rule.name}' has invalid system_regex: {exc}"
                ) from exc
        if not _TEMPLATE_ONLY.match(rule.pool) and rule.pool not in cfg.pools:
            raise ValueError(
                f"rule '{rule.name}' references unknown pool '{rule.pool}'"
            )
    c = cfg.classifier
    if c.enabled:
        if c.pool not in cfg.pools:
            raise ValueError(f"classifier.pool '{c.pool}' is not a defined pool")
        if c.fallback_pool not in cfg.pools:
            raise ValueError(
                f"classifier.fallback_pool '{c.fallback_pool}' is not a defined pool"
            )
        if not c.labels:
            raise ValueError("classifier.labels must be non-empty when enabled")
        for label in c.labels:
            if label not in cfg.pools:
                raise ValueError(
                    f"classifier label '{label}' is not a defined pool"
                )
