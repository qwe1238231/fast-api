"""`cancel_payment_intent`:回傳的是 Stripe 的**當下狀態**,不是成功與否。

呼叫端(在途付款的超時 cron)靠這個狀態決定能不能把本地訂單 EXPIRED。所以這裡每一條
測的都是「某種 Stripe 回應 → 我們讀到什麼狀態、打了幾次 API」,不碰 DB。

Stripe client 用 MagicMock,跟 conftest 替 webhook 端點裝的 `app.state.stripe` 同一套。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
import stripe

from app.services.stripe_client import cancel_payment_intent

PI = "pi_abandoned_1"


def _client(*, cancel: AsyncMock, retrieve: AsyncMock | None = None) -> MagicMock:
    client = MagicMock()
    client.v1.payment_intents.cancel_async = cancel
    client.v1.payment_intents.retrieve_async = retrieve or AsyncMock(
        side_effect=AssertionError("retrieve must not be called on this path")
    )
    return client


def _unexpected_state(*, carried_status: str | None) -> stripe.InvalidRequestError:
    """Stripe 對「這個狀態不能取消」的回應。`carried_status=None` 模擬錯誤體沒帶 intent。"""
    error = {"code": "payment_intent_unexpected_state"}
    if carried_status is not None:
        error["payment_intent"] = {"id": PI, "status": carried_status}
    return stripe.InvalidRequestError(
        "cannot cancel", "intent",
        code="payment_intent_unexpected_state", json_body={"error": error},
    )


@pytest.mark.asyncio
async def test_cancel_succeeds_and_tags_the_reason() -> None:
    """正常路徑:一次呼叫、回 canceled、帶 `abandoned` 讓儀表板分得出是我們超時收的。"""
    cancel = AsyncMock(return_value=MagicMock(status="canceled"))

    status = await cancel_payment_intent(_client(cancel=cancel), payment_intent_id=PI)

    assert status == "canceled"
    cancel.assert_awaited_once_with(PI, {"cancellation_reason": "abandoned"})


@pytest.mark.parametrize("carried", ["succeeded", "processing", "requires_action", "canceled"])
@pytest.mark.asyncio
async def test_unexpected_state_reads_status_from_the_error_body(carried: str) -> None:
    """取消被拒時,狀態直接從錯誤體讀,**不多打一次 retrieve**。

    `canceled` 也在名單裡:重複取消(上一輪取消了但本地沒寫成)Stripe 一樣回這個錯,
    而它對呼叫端的意義跟取消成功完全相同。
    """
    cancel = AsyncMock(side_effect=_unexpected_state(carried_status=carried))

    status = await cancel_payment_intent(_client(cancel=cancel), payment_intent_id=PI)

    assert status == carried


@pytest.mark.asyncio
async def test_unexpected_state_without_body_falls_back_to_retrieve() -> None:
    """錯誤體沒帶 intent 時才退回 retrieve。這條保住的是「不確定就去問,不要猜」。"""
    cancel = AsyncMock(side_effect=_unexpected_state(carried_status=None))
    retrieve = AsyncMock(return_value=MagicMock(status="succeeded"))

    status = await cancel_payment_intent(
        _client(cancel=cancel, retrieve=retrieve), payment_intent_id=PI
    )

    assert status == "succeeded"
    retrieve.assert_awaited_once_with(PI)


@pytest.mark.asyncio
async def test_other_invalid_requests_propagate() -> None:
    """`resource_missing` 之類不是狀態問題,是資料問題 —— 吞掉會讓 cron 每分鐘對一個
    不存在的 intent 靜靜重試到永遠。"""
    cancel = AsyncMock(side_effect=stripe.InvalidRequestError(
        "No such payment_intent", "intent", code="resource_missing",
    ))

    with pytest.raises(stripe.InvalidRequestError, match="No such payment_intent"):
        await cancel_payment_intent(_client(cancel=cancel), payment_intent_id=PI)


@pytest.mark.asyncio
async def test_reason_is_passed_through_to_stripe() -> None:
    """兩個呼叫端要在儀表板上分得開:T2 cron 是 abandoned,使用者取消是 requested_by_customer。"""
    cancel = AsyncMock(return_value=MagicMock(status="canceled"))

    await cancel_payment_intent(
        _client(cancel=cancel), payment_intent_id=PI, reason="requested_by_customer"
    )

    cancel.assert_awaited_once_with(PI, {"cancellation_reason": "requested_by_customer"})


@pytest.mark.asyncio
async def test_transport_errors_propagate() -> None:
    """網路層的錯不屬於這個函式的決策範圍:呼叫端看到例外就是「這一輪不知道,跳過」。"""
    cancel = AsyncMock(side_effect=stripe.APIConnectionError("boom"))

    with pytest.raises(stripe.APIConnectionError):
        await cancel_payment_intent(_client(cancel=cancel), payment_intent_id=PI)
