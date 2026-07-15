import httpx
import respx


async def test_end_to_end_rule_routing(client, auth_headers, app):
    models = await client.get("/v1/models", headers=auth_headers)
    assert {m["id"] for m in models.json()["data"]} == {"cheap", "default", "large-context"}

    # A medium request (no tools, no hint, > 500 and < 20000 tokens) → default pool.
    medium = "x" * 4000  # ~1000 tokens
    with respx.mock:
        respx.post("https://api.anthropic.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"id": "z", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
        resp = await client.post(
            "/v1/chat/completions", headers=auth_headers,
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": medium}]})
    assert resp.status_code == 200

    cur = await app.state.db.connection.execute(
        "SELECT pool, provider, route_stage, route_reason FROM requests")
    row = await cur.fetchone()
    assert row[0] == "default" and row[1] == "anthropic"
    assert row[2] == "default" and row[3] == "no-rule-matched"
