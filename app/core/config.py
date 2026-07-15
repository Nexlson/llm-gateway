from __future__ import annotations

import os

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


class AppConfig(BaseModel):
    api_key_env: str = ""     # name of the env var holding the gateway's static key
    api_key: str = ""         # resolved from os.environ[api_key_env] at load time
    db_path: str = "gateway.db"
    default_pool: str
    providers: dict[str, ProviderConfig]
    pools: dict[str, list[PoolEntry]]
    prices: dict[str, PriceEntry] = Field(default_factory=dict)


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
