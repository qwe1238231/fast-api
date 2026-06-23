import pytest


async def register_and_login(client, username="alice", password="secret123"):
    await client.post("/v1/users/", json={"username": username, "password": password})
    return await client.post(
        "/v1/auth/token", data={"username": username, "password": password}
    )


@pytest.mark.asyncio
async def test_login_sets_refresh_and_csrf_cookies(client):
    r = await register_and_login(client)
    assert r.status_code == 200
    assert client.cookies.get("refresh_token")
    assert client.cookies.get("csrf_token")


@pytest.mark.asyncio
async def test_refresh_without_csrf_is_rejected(client):
    await register_and_login(client)
    r = await client.post("/v1/auth/refresh")          # 沒帶 X-CSRF-Token header
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_refresh_rotates_token(client):
    await register_and_login(client)
    old_refresh = client.cookies.get("refresh_token")
    csrf = client.cookies.get("csrf_token")

    r = await client.post("/v1/auth/refresh", headers={"X-CSRF-Token": csrf})

    assert r.status_code == 200
    assert "access_token" in r.json()
    assert client.cookies.get("refresh_token") != old_refresh   # 輪替:新 token ≠ 舊 token


@pytest.mark.asyncio
async def test_reusing_old_token_revokes_whole_family(client):
    await register_and_login(client)
    token_a = client.cookies.get("refresh_token")

    # 第一次 refresh:A 被標記 used,拿到 B
    r1 = await client.post(
        "/v1/auth/refresh",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert r1.status_code == 200
    token_b = client.cookies.get("refresh_token")

    async def replay(token):
        # 清空 jar,只放指定的 refresh token + 一組一致的 csrf(過 CSRF 閘)
        client.cookies.clear()
        client.cookies.set("refresh_token", token, domain="test", path="/v1/auth")
        client.cookies.set("csrf_token", "x", domain="test", path="/")
        return await client.post("/v1/auth/refresh", headers={"X-CSRF-Token": "x"})

    # 重用舊的 A → 偵測到竊用 → 撤銷整個 family
    assert (await replay(token_a)).status_code == 401

    # family 已撤銷 → 連「合法的」B 現在也作廢
    assert (await replay(token_b)).status_code == 401


@pytest.mark.asyncio
async def test_logout_ends_session(client):
    await register_and_login(client)
    csrf = client.cookies.get("csrf_token")

    r = await client.post("/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204

    # 登出後 refresh 失敗(cookie 已清 + token 已撤銷)
    r2 = await client.post("/v1/auth/refresh", headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_logout_all(client):
    r = await register_and_login(client)
    token = r.json()["access_token"]

    rr = await client.post(
        "/v1/auth/logout-all", headers={"Authorization": f"Bearer {token}"}
    )
    assert rr.status_code == 204