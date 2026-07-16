import asyncio
import pytest
from app.services.inventory import (
    set_initial_stock, reserve, release, get_available,
)
from app.core.exceptions import InsufficientInventory

EVENT_ID = 1

@pytest.mark.asyncio
async def test_reserve_decrements_available(redis):
    await set_initial_stock(redis, event_id=EVENT_ID, total_seats=10)
    await reserve(redis, event_id=EVENT_ID, quantity=3)
    assert await get_available(redis, event_id=EVENT_ID) == 7

@pytest.mark.asyncio
async def test_reserve_sold_out_raises_and_rolls_back(redis):
    await set_initial_stock(redis, event_id=EVENT_ID, total_seats=2)
    with pytest.raises(InsufficientInventory):
        await reserve(redis, event_id=EVENT_ID, quantity=3)
    # 失敗要把庫存補回去,不能扣掉
    assert await get_available(redis, event_id=EVENT_ID) == 2

@pytest.mark.asyncio
async def test_release_returns_stock(redis):
    await set_initial_stock(redis, event_id=EVENT_ID, total_seats=5)
    await reserve(redis, event_id=EVENT_ID, quantity=5)
    assert await release(redis, event_id=EVENT_ID, quantity=2, marker="order:1") is True
    assert await get_available(redis, event_id=EVENT_ID) == 2
    # idempotent: same marker again is a no-op (guards double-release)
    assert await release(redis, event_id=EVENT_ID, quantity=2, marker="order:1") is False
    assert await get_available(redis, event_id=EVENT_ID) == 2

@pytest.mark.asyncio
async def test_no_oversell_under_concurrency(redis):
    STOCK = 50
    ATTEMPTS = 200          # 需求遠大於供給

    await set_initial_stock(redis, event_id=EVENT_ID, total_seats=STOCK)

    # 同時發 200 個 reserve(每個搶 1 張)
    results = await asyncio.gather(
        *[reserve(redis, event_id=EVENT_ID, quantity=1) for _ in range(ATTEMPTS)],
        return_exceptions=True,     # 失敗的回傳例外物件,而不是中斷整批
    )

    successes = [r for r in results if r is None]
    failures = [r for r in results if isinstance(r, InsufficientInventory)]

    assert len(successes) == STOCK            # 剛好賣出 50,不多不少
    assert len(failures) == ATTEMPTS - STOCK  # 其餘全被擋下
    assert await get_available(redis, event_id=EVENT_ID) == 0   # 不為負