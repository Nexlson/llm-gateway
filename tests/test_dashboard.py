from datetime import UTC, datetime


async def test_dashboard_requires_auth(client):
    resp = await client.get("/dashboard")
    assert resp.status_code == 401


async def test_dashboard_empty_state_behind_auth(client, auth_headers):
    resp = await client.get("/dashboard", headers=auth_headers)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["cache-control"] == "no-store"
    assert "Gateway dashboard" in resp.text
    assert "Today" in resp.text
    assert "This week" in resp.text
    assert "This month" in resp.text
    assert "No requests recorded." in resp.text
    assert "Not configured" in resp.text
    assert "Coming in M5" not in resp.text
    assert "exclude internal classifier probe calls" in resp.text


async def test_dashboard_renders_stored_rows_and_escapes_text(
    app, client, auth_headers
):
    now = int(datetime.now(UTC).timestamp())
    await app.state.db.connection.execute(
        """
        INSERT INTO requests (
          id, ts, pool, provider, model, route_stage, route_reason,
          input_tokens, output_tokens, cost_usd, latency_ms, status, fallback_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "dash-1", now, "cheap", "deepseek", "deepseek-chat", "fallback",
            "<script>alert(1)</script>", 100, 25, 0.0012, 42, 200,
            "evil/<primary>",
        ),
    )
    await app.state.db.connection.commit()

    resp = await client.get("/dashboard", headers=auth_headers)

    assert resp.status_code == 200
    assert "deepseek-chat" in resp.text
    assert "cheap" in resp.text
    assert "125" in resp.text
    assert "evil/&lt;primary&gt;" in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text
    assert "<script>alert(1)</script>" not in resp.text
    assert "Cooldown trips are not separately recorded" in resp.text
