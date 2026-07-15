from __future__ import annotations

import httpx

from app.core.config import AppConfig
from app.providers.base import ProviderAdapter
from app.providers.openai_compatible import OpenAICompatibleAdapter


def build_registry(config: AppConfig, http_client: httpx.AsyncClient) -> dict[str, ProviderAdapter]:
    return {
        name: OpenAICompatibleAdapter(name, provider_cfg, http_client)
        for name, provider_cfg in config.providers.items()
    }
