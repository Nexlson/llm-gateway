import json

import httpx
import respx


async def test_requires_auth(client):
    resp = await client.post("/v1/chat/completions", json={"model": "cheap"})
    assert resp.status_code == 401


async def test_missing_model_is_400(client, auth_headers):
    resp = await client.post("/v1/chat/completions", json={"messages": []},
                             headers=auth_headers)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["param"] == "model"


@respx.mock
async def test_passthrough_rewrites_model_and_forwards_unknown_fields(client, auth_headers):
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"id": "resp-1", "choices": [{"message": {"content": "hi"}}],
                  "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        )
    )
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "cheap", "messages": [{"role": "user", "content": "hi"}],
              "temperature": 0.2, "future_field": True},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "resp-1"                 # body unchanged
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "deepseek-chat"              # rewritten
    assert sent["temperature"] == 0.2                    # preserved
    assert sent["future_field"] is True                  # unknown forwarded


@respx.mock
async def test_upstream_500_returns_openai_error(client, auth_headers):
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"error": {"message": "down"}})
    )
    resp = await client.post(
        "/v1/chat/completions", headers=auth_headers,
        json={"model": "cheap", "messages": []},
    )
    assert resp.status_code == 500
    assert "error" in resp.json()


@respx.mock
async def test_writes_one_request_row(client, auth_headers, app):
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"id": "r", "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
    )
    await client.post("/v1/chat/completions", headers=auth_headers,
                      json={"model": "cheap", "messages": []})
    conn = app.state.db.connection
    cur = await conn.execute(
        "SELECT pool, provider, model, route_stage, input_tokens, output_tokens, "
        "cost_usd, status, fallback_from FROM requests")
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0] == ("cheap", "deepseek", "deepseek-chat", "passthrough", 4, 2, 0.0, 200, None)
