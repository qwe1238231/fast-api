import pytest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models.order import Order, OrderStatus
from app.models.user import User
from app.services.inventory import reserve, get_available
from app.worker import PENDING_TIMEOUT_MINUTES, expire_pending_orders

#: 剛好過了 T1 一分鐘。從常數推而不是寫死 11:T1 現在是 Setting,寫死的話改設定後
#: 這兩條會靜靜地測到錯的邊界。
PAST_T1 = timedelta(minutes=PENDING_TIMEOUT_MINUTES + 1)


@pytest.mark.asyncio
async def test_expire_pending_releases_inventory(db, redis, published_event):
    # Arrange:一個 user、reserve 2 張、建一筆剛過 T1 的 PENDING 訂單
    user = User(username="late", hashed_password="x")
    db.add(user)
    await db.flush()

    await reserve(redis, event_id=published_event.id, quantity=2)        # 5 → 3
    old = datetime.now(timezone.utc) - PAST_T1                           # 超過 T1 逾時
    order = Order(
        user_id=user.id, event_id=published_event.id, quantity=2,
        total_price_cents=3000, idempotency_key=uuid4(),
        status=OrderStatus.PENDING, created_at=old,
    )
    db.add(order)
    await db.commit()

    # Act:跑過期任務(傳一個帶 redis_client 的 ctx,模仿 arq)
    await expire_pending_orders({"redis_client": redis})

    # Assert:訂單變 EXPIRED,票釋放回庫存
    await db.refresh(order)
    assert order.status == OrderStatus.EXPIRED
    assert await get_available(redis, event_id=published_event.id) == 5  # 3 → 5


@pytest.mark.asyncio
async def test_expire_skips_orders_with_payment_intent(db, redis, published_event):
    """An order in the Stripe flow (payment_provider_id set) is NOT expired by the
    timeout cron even past the cutoff — its lifecycle is driven by the payment
    webhooks, so a boundary-time payer isn't expired out from under a charge."""
    user = User(username="paying", hashed_password="x")
    db.add(user)
    await db.flush()

    await reserve(redis, event_id=published_event.id, quantity=1)        # 5 → 4
    old = datetime.now(timezone.utc) - PAST_T1                           # past the T1 cutoff
    order = Order(
        user_id=user.id, event_id=published_event.id, quantity=1,
        total_price_cents=1500, idempotency_key=uuid4(),
        status=OrderStatus.PENDING, created_at=old,
        payment_provider_id="pi_test_123",                              # in the Stripe flow
    )
    db.add(order)
    await db.commit()

    await expire_pending_orders({"redis_client": redis})

    await db.refresh(order)
    assert order.status == OrderStatus.PENDING                           # NOT expired
    assert await get_available(redis, event_id=published_event.id) == 4  # seat still held