from __future__ import annotations

import httpx

from app.core.config import ProviderConfig
from app.providers.base import ProviderError, ProviderResponse, is_retryable


class OpenAICompatibleAdapter:
    def __init__(self, name: str, config: ProviderConfig, http_client: httpx.AsyncClient) -> None:
        self.name = name
        self._config = config
        self._client = http_client

    async def chat_completion(self, payload: dict, model: str) -> ProviderResponse:
        upstream = {**payload, "model": model}
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        try:
            resp = await self._client.post(
                url, json=upstream, headers=headers, timeout=self._config.timeout_s
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"provider '{self.name}' timed out", 504, self.name, retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"provider '{self.name}' request failed: {exc}", 502, self.name,
                retryable=True,
            ) from exc

        if resp.status_code >= 400:
            message = _extract_message(resp) or f"provider '{self.name}' error"
            raise ProviderError(
                message, resp.status_code, self.name,
                retryable=is_retryable(resp.status_code),
            )

        body = resp.json()
        usage = body.get("usage") or {}
        return ProviderResponse(
            status_code=resp.status_code,
            body=body,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )


def _extract_message(resp: httpx.Response) -> str | None:
    try:
        data = resp.json()
    except Exception:
        return None
    err = data.get("error")
    if isinstance(err, dict):
        return err.get("message")
    return None
