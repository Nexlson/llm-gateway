async def test_requires_auth(client):
    resp = await client.get("/v1/models")
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


async def test_lists_pools_as_models(client, auth_headers):
    resp = await client.get("/v1/models", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    ids = {m["id"] for m in data["data"]}
    assert ids == {"cheap", "default", "large-context"}
    assert all(m["object"] == "model" for m in data["data"])
