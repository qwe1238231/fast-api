"""付款路徑的兩道「免費拿票」通道,以及擋住它們的 fail-closed 守衛。

存在的理由是這兩個洞都**不需要任何漏洞利用技巧**,只要設定檔沒填就自動打開:

1. `STRIPE_WEBHOOK_SECRET` 預設是空字串。空字串不是「沒有密鑰」而是「密鑰的值是
   空字串」—— 攻擊者拿它算 HMAC 就能偽造 `payment_intent.succeeded`,把任何訂單
   推成 CONFIRMED。而且**部署當下這個值本來就是空的**(infra 沒注入)。
2. `/orders/{id}/pay` 是模擬付款,完全不經過 Stripe。上線就等於任何登入使用者可以
   零元把自己的訂單推成 CONFIRMED 並拿到座號。

`test_webhook.py` 每一條都 monkeypatch 掉 `stripe.Webhook.construct_event`,所以它
**在結構上不可能**發現第 1 點 —— 它測的是「處理函式怎麼對待驗簽結果」,不是「驗簽
到底有沒有發生」。這個檔案補的就是那一層:真的密鑰、真的簽章、不打補丁。
"""
import hashlib
import hmac
import json
import time
from uuid import uuid4

import pytest
import stripe
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.security import create_admission_token
from app.models.user import User

TEST_SECRET = "whsec_test_do_not_use_in_production"

# 完整的 Stripe 事件信封。`"object": "event"` 是必要的 —— stripe 函式庫靠它決定要把
# JSON 轉成哪個類別,少了就在驗簽成功之後才炸,那個 500 會被誤讀成「驗簽壞了」。
# metadata 留空 → 處理函式找不到 order_id 就忽略,所以這些測試不需要真的訂單。
PAYLOAD = json.dumps(
    {
        "id": "evt_test_guard",
        "object": "event",
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_test_guard", "object": "payment_intent",
                            "metadata": {}, "amount_received": 0}},
    }
).encode()


def _sign(payload: bytes, *, secret: str, timestamp: int | None = None) -> str:
    """組出 Stripe 的 `Stripe-Signature` 標頭。

    格式是 `t=<unix>,v1=<hex>`,其中 hex 是 HMAC-SHA256(secret, "<t>.<payload>")。
    自己算而不是用 stripe 的工具,是為了讓「攻擊者手上有什麼」這件事在測試裡是明的:
    他有 payload、有時間、有(他猜的)密鑰,沒別的。
    """
    ts = int(time.time()) if timestamp is None else timestamp
    signed = f"{ts}.".encode() + payload
    mac = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


# ─ 設定守衛:危險開關一律 fail-closed

#: 一個已知安全的正式設定。測試從這裡出發,一次只改要測的那一項。
PRODUCTION_BASELINE = {
    "DEBUG": False,
    "STRIPE_WEBHOOK_SECRET": TEST_SECRET,
    "ENABLE_MOCK_PAYMENT": False,
    "LOADTEST_BYPASS_ADMISSION": False,
}


def _settings(**overrides: object) -> Settings:
    """明確給定每一個危險開關,不要繼承環境。

    `Settings` 會讀 `.env`,而開發者的 `.env` 現在有 `ENABLE_MOCK_PAYMENT=True`。
    不寫死基線的話,這些測試量到的是那台機器的設定而不是守衛的行為 —— 本機綠、CI 紅
    (或更糟:本機紅、CI 綠)。
    """
    return Settings(**(PRODUCTION_BASELINE | overrides))


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"STRIPE_WEBHOOK_SECRET": ""}, id="empty-webhook-secret"),
        pytest.param({"ENABLE_MOCK_PAYMENT": True}, id="mock-payment"),
        pytest.param({"LOADTEST_BYPASS_ADMISSION": True}, id="admission-bypass"),
    ],
)
def test_the_app_refuses_to_boot_in_a_giveaway_configuration(override) -> None:
    """設定錯誤要在**啟動時**炸,不是在被盜刷之後才發現。

    三個開關放同一條測試是刻意的:它們是同一個規則的三個實例(危險能力一律綁 DEBUG,
    而且拒絕啟動而不是印警告 —— 警告在 ECS 的 log 裡沒有人會看到)。下次再加第四個
    開關時,這個 parametrize 就是它該長什麼樣子的規格。
    """
    with pytest.raises(ValidationError):
        _settings(**override)


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({}, id="production"),                    # 密鑰有填,開關全關
        pytest.param({"DEBUG": True, "STRIPE_WEBHOOK_SECRET": ""}, id="dev-no-secret"),
        pytest.param({"DEBUG": True, "ENABLE_MOCK_PAYMENT": True}, id="dev-mock-pay"),
        pytest.param({"DEBUG": True, "LOADTEST_BYPASS_ADMISSION": True}, id="dev-k6"),
    ],
)
def test_the_legitimate_configurations_still_boot(override) -> None:
    """守衛不能寬到擋住正常用法 —— 否則下一步就是有人把它註解掉。"""
    assert _settings(**override) is not None


# ─ 驗簽這件事「真的有發生」

def test_an_empty_secret_accepts_a_forged_event() -> None:
    """守衛存在的理由,寫成可執行的形式。

    這條**故意證明漏洞成立**:用空密鑰簽的偽造事件會通過驗證。攻擊者不需要知道任何
    祕密,因為空字串大家都知道。如果哪天它紅了,代表 Stripe 自己開始拒絕空密鑰,
    那時才可以討論放寬上面的 Settings 守衛。
    """
    forged = stripe.Webhook.construct_event(
        payload=PAYLOAD, sig_header=_sign(PAYLOAD, secret=""), secret=""
    )
    assert forged["type"] == "payment_intent.succeeded"


@pytest.mark.asyncio
async def test_a_forged_signature_is_rejected_by_the_real_verifier(
    client, monkeypatch
) -> None:
    """密鑰有填時,拿別的密鑰簽的請求要被擋。

    不 monkeypatch `construct_event` 是重點:這條走的是 stripe 函式庫真正的驗簽路徑,
    所以它證明的是「端點確實驗簽」,而不是「端點會處理驗簽拋出的例外」。
    """
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", TEST_SECRET)

    r = await client.post(
        "/v1/webhooks/stripe",
        content=PAYLOAD,
        headers={"Stripe-Signature": _sign(PAYLOAD, secret="whsec_attacker_guess")},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_a_correctly_signed_event_is_accepted(client, monkeypatch) -> None:
    """反向對照。少了它,上一條可以靠「端點永遠回 400」通過。"""
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", TEST_SECRET)

    r = await client.post(
        "/v1/webhooks/stripe",
        content=PAYLOAD,
        headers={"Stripe-Signature": _sign(PAYLOAD, secret=TEST_SECRET)},
    )
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_a_replayed_signature_expires(client, monkeypatch) -> None:
    """簽章正確但時間戳太舊 → 擋。錄下一次成功的 webhook 就能無限重播,而重播
    `payment_intent.succeeded` 對已退款的訂單意義完全不同。"""
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", TEST_SECRET)
    stale = _sign(PAYLOAD, secret=TEST_SECRET, timestamp=int(time.time()) - 86_400)

    r = await client.post(
        "/v1/webhooks/stripe", content=PAYLOAD, headers={"Stripe-Signature": stale}
    )
    assert r.status_code == 400


# ─ 模擬付款端點的開關

async def _own_a_pending_order(client, db, drain, event_id: int) -> tuple[int, dict]:
    """註冊 → 下單 → 讓 worker 落帳,回 (order_id, auth headers)。"""
    await client.post("/v1/users/", json={"username": "payer", "password": "secret123"})
    token = (
        await client.post(
            "/v1/auth/token", data={"username": "payer", "password": "secret123"}
        )
    ).json()["access_token"]
    user_id = await db.scalar(select(User.id).where(User.username == "payer"))
    await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 1},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid4()),
            "Admission-Token": create_admission_token(
                user_id=user_id, event_id=event_id, ttl_seconds=120
            ),
        },
    )
    await drain()
    auth = {"Authorization": f"Bearer {token}"}
    order_id = (await client.get("/v1/orders/me", headers=auth)).json()["items"][0]["id"]
    return order_id, auth


@pytest.mark.asyncio
async def test_mock_payment_is_invisible_when_disabled(
    client, db, published_event, drain_orders, monkeypatch
) -> None:
    """關掉時對**自己的、確實存在的** PENDING 訂單也要回 404。

    用自己的訂單是這條測試的關鍵:換成不存在的訂單編號,開關開或關都會回 404,
    測試就永遠是綠的而什麼都沒證明。
    """
    order_id, auth = await _own_a_pending_order(
        client, db, drain_orders, published_event.id
    )
    monkeypatch.setattr(get_settings(), "ENABLE_MOCK_PAYMENT", False)

    r = await client.post(f"/v1/orders/{order_id}/pay", headers=auth)
    assert r.status_code == 404
    assert r.json()["detail"] == "Not Found", "不要洩漏這個端點存在"

    got = await client.get(f"/v1/orders/{order_id}", headers=auth)
    assert got.json()["status"] == "pending", "訂單狀態不能被動到"


@pytest.mark.asyncio
async def test_mock_payment_still_works_when_enabled(
    client, db, published_event, drain_orders
) -> None:
    """開著時照舊 —— 本機開發與 k6 壓測都靠這條路徑。"""
    order_id, auth = await _own_a_pending_order(
        client, db, drain_orders, published_event.id
    )
    assert get_settings().ENABLE_MOCK_PAYMENT, "測試環境應該是開的(conftest 設定)"

    r = await client.post(f"/v1/orders/{order_id}/pay", headers=auth)
    assert r.status_code == 204
    got = await client.get(f"/v1/orders/{order_id}", headers=auth)
    assert got.json()["status"] == "confirmed"
