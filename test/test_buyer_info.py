import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import BuyerInfoAlreadyExists
from app.crud.user import get_user_by_username
from app.services.buyer_info import register_buyer_info


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
    r = await client.post("/v1/buyer-info/", json=payload, headers=headers)
    assert r.status_code == 409
    # 是「你已經填過了」(帶 user_id),不是「這張證是別人的」。
    assert "user_id" in r.json()


@pytest.mark.asyncio
async def test_same_national_id_from_another_user_returns_409(client):
    """一張身分證只能綁一個帳號:第二個人拿同一張證來註冊,撞的是
    ix_buyer_info_national_id_lookup_hash。回的是通用訊息 —— 不洩漏是哪個帳號佔了它。"""
    payload = {"national_id": "A123456789", "real_name": "Wang Xiaoming"}
    alice = await auth_headers(client, "alice")
    bob = await auth_headers(client, "bob")

    assert (await client.post("/v1/buyer-info/", json=payload, headers=alice)).status_code == 201
    r = await client.post(
        "/v1/buyer-info/", json={**payload, "real_name": "Bob"}, headers=bob
    )
    assert r.status_code == 409
    assert r.json() == {"detail": "National ID is already registered"}


@pytest.mark.asyncio
async def test_losing_the_race_on_user_id_is_reported_as_already_exists(
    client, db, monkeypatch
):
    """預檢 SELECT 與 INSERT 之間的縫:另一個請求已經替同一個 user 寫進去了。
    這時撞的是 buyer_info_pkey,不是身分證索引。以前兩種都被報成
    NationalIdAlreadyRegistered,把「你已經填過了」講成「這張證是別人的」。"""
    alice_headers = await auth_headers(client, "alice")
    payload = {"national_id": "A123456789", "real_name": "Wang Xiaoming"}
    assert (await client.post("/v1/buyer-info/", json=payload, headers=alice_headers)).status_code == 201
    # 先取成 int:service 攔到 IntegrityError 會 rollback,那會 expire session 裡的
    # 所有物件,之後再讀 alice.id 就是一次 lazy load —— async 底下等於 MissingGreenlet。
    alice_id = (await get_user_by_username(db, "alice")).id

    # 模擬輸掉競賽:預檢看到的是「還沒有」,INSERT 時列已經在了。
    async def nothing_yet(db, user_id):
        return None

    monkeypatch.setattr("app.services.buyer_info.get_buyer_info_by_user_id", nothing_yet)

    # 用一張**不同**的身分證,讓唯一能撞的只有 PK —— 把分流的兩條路徑隔開。
    with pytest.raises(BuyerInfoAlreadyExists) as excinfo:
        await register_buyer_info(
            db, user_id=alice_id, real_name="Wang Xiaoming", national_id="B987654321"
        )
    assert excinfo.value.user_id == alice_id


@pytest.mark.asyncio
async def test_unrelated_integrity_errors_are_not_disguised_as_409(db):
    """分流只認那兩個約束名。外鍵違反(user 不存在)不是使用者的錯,是呼叫端的 bug,
    要原樣往上丟,不能被翻成 409。"""
    with pytest.raises(IntegrityError):
        await register_buyer_info(
            db, user_id=999_999, real_name="Nobody", national_id="C111111111"
        )
