async def test_dashboard_requires_auth(client):
    resp = await client.get("/dashboard")
    assert resp.status_code == 401


async def test_dashboard_stub_behind_auth(client, auth_headers):
    resp = await client.get("/dashboard", headers=auth_headers)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "M5" in resp.text
