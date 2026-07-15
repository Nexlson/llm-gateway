import json

import httpx
import respx


async def test_requires_auth(client):
    resp = await client.post("/v1/chat/completions", json={"model": "x"})
    assert resp.status_code == 401


async def test_missing_model_is_400(client, auth_headers):
    resp = await client.post("/v1/chat/completions", json={"messages": []},
                             headers=auth_headers)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["param"] == "model"


@respx.mock
async def test_short_request_routed_to_cheap_and_model_rewritten(client, auth_headers):
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"id": "resp-1", "choices": [{"message": {"content": "hi"}}],
                       "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    )
    resp = await client.post(
        "/v1/chat/completions", headers=auth_headers,
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
              "temperature": 0.2, "future_field": True},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "resp-1"                 # body unchanged
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "deepseek-chat"              # rewritten to resolved model
    assert sent["temperature"] == 0.2                    # preserved
    assert sent["future_field"] is True                  # unknown forwarded


@respx.mock
async def test_header_hint_overrides_routing(client, auth_headers):
    respx.post("https://api.anthropic.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"id": "z"}))
    resp = await client.post(
        "/v1/chat/completions",
        headers={**auth_headers, "x-pool": "default"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200                        # routed to default (anthropic)


@respx.mock
async def test_writes_row_with_route_and_cost(client, auth_headers, app):
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"id": "r", "usage": {"prompt_tokens": 1_000_000,
                                            "completion_tokens": 1_000_000}}))
    await client.post("/v1/chat/completions", headers=auth_headers,
                      json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    cur = await app.state.db.connection.execute(
        "SELECT pool, provider, model, route_stage, route_reason, cost_usd, status "
        "FROM requests")
    rows = await cur.fetchall()
    assert len(rows) == 1
    pool, provider, model, stage, reason, cost, status = rows[0]
    assert (pool, provider, model, stage) == ("cheap", "deepseek", "deepseek-chat", "rule")
    assert reason == "rule:short-and-simple"
    assert round(cost, 6) == round(0.27 + 1.10, 6)       # cost from provider usage
    assert status == 200


@respx.mock
async def test_upstream_500_returns_openai_error(client, auth_headers):
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"error": {"message": "down"}}))
    resp = await client.post(
        "/v1/chat/completions", headers=auth_headers,
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 500
    assert "error" in resp.json()
