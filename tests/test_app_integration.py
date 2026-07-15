import httpx
import respx


async def test_end_to_end_flow(client, auth_headers, app):
    # models
    models = await client.get("/v1/models", headers=auth_headers)
    assert {m["id"] for m in models.json()["data"]} == {"cheap", "default", "large-context"}

    # chat via default pool (unknown model name -> default_pool)
    with respx.mock:
        respx.post("https://api.anthropic.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"id": "z", "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        )
        resp = await client.post("/v1/chat/completions", headers=auth_headers,
                                 json={"model": "gpt-4o", "messages": []})
    assert resp.status_code == 200

    cur = await app.state.db.connection.execute(
        "SELECT pool, provider, route_reason FROM requests")
    row = await cur.fetchone()
    assert row[0] == "default" and row[1] == "anthropic"
    assert "model-not-a-pool" in row[2]
