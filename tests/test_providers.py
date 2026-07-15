import httpx
import pytest
import respx

from app.core.config import ProviderConfig
from app.providers.base import ProviderError
from app.providers.openai_compatible import OpenAICompatibleAdapter


def _adapter(client):
    cfg = ProviderConfig(base_url="https://api.deepseek.com/v1", api_key="sk-test")
    return OpenAICompatibleAdapter("deepseek", cfg, client)


@respx.mock
async def test_forwards_and_rewrites_model():
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"id": "x", "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        )
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        resp = await adapter.chat_completion(
            {"model": "cheap", "messages": [{"role": "user", "content": "hi"}],
             "future_field": True},
            model="deepseek-chat",
        )
    sent = route.calls.last.request
    body = __import__("json").loads(sent.content)
    assert body["model"] == "deepseek-chat"          # rewritten
    assert body["future_field"] is True               # unknown field forwarded
    assert sent.headers["authorization"] == "Bearer sk-test"
    assert resp.status_code == 200
    assert (resp.input_tokens, resp.output_tokens) == (7, 3)


@respx.mock
async def test_5xx_raises_retryable_provider_error():
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": {"message": "down"}})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderError) as ei:
            await _adapter(client).chat_completion({"model": "x"}, model="deepseek-chat")
    assert ei.value.status_code == 503 and ei.value.retryable is True


@respx.mock
async def test_400_raises_nonretryable_provider_error():
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad"}})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderError) as ei:
            await _adapter(client).chat_completion({"model": "x"}, model="deepseek-chat")
    assert ei.value.status_code == 400 and ei.value.retryable is False
