import pytest
from app.services.idempotency import try_claim, get_claimed_order_id


@pytest.mark.asyncio
async def test_unclaimed_key_returns_none(redis):
    assert await get_claimed_order_id(redis, idempotency_key="never-seen") is None

@pytest.mark.asyncio
async def test_claim_is_idempotent(redis):
    key = "abc-123"
    assert await try_claim(redis, idempotency_key=key, order_id=1) is True    # 第一次成功
    assert await try_claim(redis, idempotency_key=key, order_id=99) is False   # 重複擋下
    assert await get_claimed_order_id(redis, idempotency_key=key) == 1         # 不被 99 覆蓋