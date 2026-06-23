import pytest


async def auth_headers(client, username="alice"):
    await client.post("/v1/users/", json={"username": username, "password": "secret123"})
    r = await client.post(
        "/v1/auth/token", data={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_register_and_retrieve_buyer_info(client):
    headers = await auth_headers(client)
    payload = {"national_id": "A123456789", "real_name": "Wang Xiaoming"}

    r = await client.post("/v1/buyer-info/", json=payload, headers=headers)
    assert r.status_code == 201
    assert r.json()["national_id"] == "A123456789"

    # 取回:PII 解密後等於原值 → 證明加密存進去又解得回來
    rget = await client.get("/v1/buyer-info/me", headers=headers)
    assert rget.status_code == 200
    assert rget.json()["national_id"] == "A123456789"
    assert rget.json()["real_name"] == "Wang Xiaoming"


@pytest.mark.asyncio
async def test_duplicate_buyer_info_returns_409(client):
    headers = await auth_headers(client)
    payload = {"national_id": "A123456789", "real_name": "Wang Xiaoming"}

    assert (await client.post("/v1/buyer-info/", json=payload, headers=headers)).status_code == 201
    assert (await client.post("/v1/buyer-info/", json=payload, headers=headers)).status_code == 409
