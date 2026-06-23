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