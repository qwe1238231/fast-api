"""Redis free-run 結構與 CAS 佔位。

最重要的是最後一組:差異測試。同一串隨機的佔位/釋放序列同時跑過 Redis+Lua 與
app.services.seating 的純函式,每一步比對完整狀態。純函式有 53 個測試守著,所以
它是 Lua 的 oracle —— 這是唯一能確信那兩支腳本正確的方法。
"""
import random
from uuid import uuid4

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select

from app.models.event import Event, EventStatus
from app.models.seating import SeatBlock, SeatHold, Zone
from app.models.user import User
from app.models.order import Order, OrderStatus
from app.scripts.seed_venue import RowSpec, VenueSpec, ZoneSpec, odd_even_labels, seed_venue
from app.services.inventory import ORDER_STREAM_KEY, _key as _event_available_key
from app.core.exceptions import NoSeatsAvailable
from app.services.seat_runs import (
    MAX_TICKETS_PER_ORDER,
    _CLAIM_SEATS_LUA,
    _ends_key,
    _geom_key,
    _runs_key,
    _zone_available_key,
    read_zone_state,
    rebuild_zone_runs,
    release_seats,
    reserve_seats_and_enqueue,
    seat_labels,
)
from app.services.seating import NORMAL_POLICY, Placement, Run, occupy, release

pytestmark = pytest.mark.asyncio


async def _make_event(db, venue_id: int, total_seats: int) -> Event:
    now = datetime.now(timezone.utc)
    event = Event(
        name="Seated", venue="X", venue_id=venue_id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=total_seats, price_cents=100, status=EventStatus.PUBLISHED,
    )
    db.add(event)
    await db.flush()
    return event


@pytest_asyncio.fixture
async def zone_fixture(db, redis):
    """一個 zone:2 排 × (6, 12, 6) = 48 席,單雙號分邊的門牌。"""
    spec = VenueSpec(
        name="Runs Arena",
        zones=(
            ZoneSpec(
                name="測試區", display_order=0,
                rows=(RowSpec("A", (6, 12, 6)), RowSpec("B", (6, 12, 6))),
                labeller=odd_even_labels,
            ),
        ),
    )
    venue = await seed_venue(db, spec)
    zone_id = await db.scalar(select(Zone.id).where(Zone.venue_id == venue.id))
    event = await _make_event(db, venue.id, 48)
    user = User(username="runs", hashed_password="x")
    db.add(user)
    await db.commit()

    await redis.set(_event_available_key(event.id), 48)
    remaining = await rebuild_zone_runs(db, redis, event_id=event.id, zone_id=zone_id)
    return {"event": event, "zone_id": zone_id, "user": user, "remaining": remaining}


async def _redis_runs(redis, event_id: int, zone_id: int) -> list[Run]:
    raw = await redis.hgetall(_runs_key(event_id, zone_id))
    runs = []
    for field, length in raw.items():
        block_id, start = field.split(":")
        runs.append(Run(int(block_id), int(start), int(length)))
    return sorted(runs, key=lambda r: (r.block_id, r.start))


async def _assert_indexes_agree(redis, event_id: int, zone_id: int) -> None:
    """runs 與 ends 必須互為索引。這是結構損壞最早的訊號 —— 比等到 DB 的 EXCLUDE
    拒絕落帳早得多,所以它也該進 detect_inventory_drift。"""
    runs = await redis.hgetall(_runs_key(event_id, zone_id))
    ends = await redis.hgetall(_ends_key(event_id, zone_id))
    expected = {}
    for field, length in runs.items():
        block_id, start = field.split(":")
        expected[f"{block_id}:{int(start) + int(length)}"] = start
    assert ends == expected, "ends 與 runs 不一致"


# ─ 重建

async def test_rebuild_gives_one_full_run_per_block(db, redis, zone_fixture) -> None:
    event, zone_id = zone_fixture["event"], zone_fixture["zone_id"]
    blocks = (await db.scalars(select(SeatBlock).where(SeatBlock.zone_id == zone_id))).all()

    runs = await _redis_runs(redis, event.id, zone_id)
    assert len(runs) == len(blocks) == 6
    assert sum(run.length for run in runs) == 48
    assert all(run.start == 0 for run in runs)
    assert int(await redis.get(_zone_available_key(event.id, zone_id))) == 48
    await _assert_indexes_agree(redis, event.id, zone_id)


async def test_rebuild_takes_the_complement_of_existing_holds(db, redis, zone_fixture) -> None:
    """DB 有 hold 時,重建必須算出補集 —— 這是 Redis 遺失後的權威復原。"""
    event, zone_id = zone_fixture["event"], zone_fixture["zone_id"]
    block = await db.scalar(
        select(SeatBlock).where(SeatBlock.zone_id == zone_id, SeatBlock.capacity == 12)
    )
    order = Order(
        user_id=zone_fixture["user"].id, event_id=event.id, zone_id=zone_id,
        quantity=4, total_price_cents=400, status=OrderStatus.PENDING,
        idempotency_key=uuid4(),
    )
    db.add(order)
    await db.flush()
    db.add(SeatHold(
        event_id=event.id, block_id=block.id, order_id=order.id,
        start_pos=4, length=4,
    ))
    await db.commit()

    remaining = await rebuild_zone_runs(db, redis, event_id=event.id, zone_id=zone_id)
    assert remaining == 44
    runs = await _redis_runs(redis, event.id, zone_id)
    in_block = [r for r in runs if r.block_id == block.id]
    assert in_block == [Run(block.id, 0, 4), Run(block.id, 8, 4)]
    await _assert_indexes_agree(redis, event.id, zone_id)


async def test_read_zone_state_round_trips_geometry(db, redis, zone_fixture) -> None:
    event, zone_id = zone_fixture["event"], zone_fixture["zone_id"]
    state = await read_zone_state(redis, event_id=event.id, zone_id=zone_id)
    blocks = {
        b.id: b
        for b in (await db.scalars(select(SeatBlock).where(SeatBlock.zone_id == zone_id))).all()
    }
    assert set(state.geometry) == set(blocks)
    for block_id, geo in state.geometry.items():
        assert geo.capacity == blocks[block_id].capacity
        assert geo.seat_quality(0) == pytest.approx(blocks[block_id].quality_edge)
        assert geo.decay >= 0                     # 單峰性守衛沒被浮點往返破壞


# ─ CAS 佔位

async def test_reserve_decrements_both_counters_and_enqueues(db, redis, zone_fixture) -> None:
    event, zone_id = zone_fixture["event"], zone_fixture["zone_id"]
    reserved = await reserve_seats_and_enqueue(
        redis, event_id=event.id, zone_id=zone_id,
        user_id=zone_fixture["user"].id, quantity=3,
        total_price_cents=300, idempotency_key=str(uuid4()),
    )
    assert reserved is not None
    assert reserved.length == 3
    assert reserved.zone_remaining == 45
    # event 級計數器也要扣 —— 等候室的 sold_out 訊號讀的是它。
    assert int(await redis.get(_event_available_key(event.id))) == 45

    entries = await redis.xrange(ORDER_STREAM_KEY)
    assert len(entries) == 1
    fields = entries[0][1]
    assert int(fields["block_id"]) == reserved.block_id
    assert int(fields["start_pos"]) == reserved.start_pos
    assert int(fields["quantity"]) == 3
    assert int(fields["zone_id"]) == zone_id
    await _assert_indexes_agree(redis, event.id, zone_id)


async def test_reserve_is_idempotent_per_key(db, redis, zone_fixture) -> None:
    event, zone_id = zone_fixture["event"], zone_fixture["zone_id"]
    key = str(uuid4())
    first = await reserve_seats_and_enqueue(
        redis, event_id=event.id, zone_id=zone_id, user_id=1, quantity=2,
        total_price_cents=200, idempotency_key=key,
    )
    second = await reserve_seats_and_enqueue(
        redis, event_id=event.id, zone_id=zone_id, user_id=1, quantity=2,
        total_price_cents=200, idempotency_key=key,
    )
    assert first is not None
    assert second is None, "重放同一個 key 必須是 no-op"
    assert int(await redis.get(_zone_available_key(event.id, zone_id))) == 46


async def test_reserve_places_the_group_contiguously(db, redis, zone_fixture) -> None:
    event, zone_id = zone_fixture["event"], zone_fixture["zone_id"]
    reserved = await reserve_seats_and_enqueue(
        redis, event_id=event.id, zone_id=zone_id, user_id=1, quantity=4,
        total_price_cents=400, idempotency_key=str(uuid4()),
    )
    assert reserved is not None
    labels = await seat_labels(
        db, block_id=reserved.block_id, start_pos=reserved.start_pos,
        length=reserved.length,
    )
    assert len(labels) == 4, "4 張票必須拿到 4 個連續 pos 的座位"


async def test_stale_run_is_rejected_by_the_cas(db, redis, zone_fixture) -> None:
    """直接用一份過時的 (run_start, run_length) 打腳本 → RETRY。

    這是 CAS 的核心判斷:空段的 (start, length) 沒變 ⟹ 整段仍全空。所以拿一份
    宣稱長度 999 的空段來要位子必須被擋。
    """
    event, zone_id = zone_fixture["event"], zone_fixture["zone_id"]
    block_id = min((await _redis_runs(redis, event.id, zone_id)), key=lambda r: r.block_id).block_id
    script = redis.register_script(_CLAIM_SEATS_LUA)
    result = await script(
        keys=[
            _runs_key(event.id, zone_id), _ends_key(event.id, zone_id),
            "claim:never", ORDER_STREAM_KEY,
            _zone_available_key(event.id, zone_id), _event_available_key(event.id),
        ],
        args=[block_id, 0, 999, 0, 2, 60, 1, event.id, 200, "never", zone_id],
        client=redis,
    )
    assert result[0] == "RETRY"
    assert int(await redis.get(_zone_available_key(event.id, zone_id))) == 48


async def test_no_fit_reports_the_feasible_quantities(db, redis) -> None:
    """只剩一段 5 連號時 4 張配不出來(會留孤兒),但 1/2/3/5 張可以。

    這種情況必須跟「售完」分開:明明看得到 5 個空位卻被告知售完是客服災難。
    """
    spec = VenueSpec(
        name="Tiny Arena",
        zones=(ZoneSpec(name="小區", display_order=0, rows=(RowSpec("A", (5,)),)),),
    )
    venue = await seed_venue(db, spec)
    zone_id = await db.scalar(select(Zone.id).where(Zone.venue_id == venue.id))
    event = await _make_event(db, venue.id, 5)
    await db.commit()
    await redis.set(_event_available_key(event.id), 5)
    await rebuild_zone_runs(db, redis, event_id=event.id, zone_id=zone_id)

    with pytest.raises(NoSeatsAvailable) as excinfo:
        await reserve_seats_and_enqueue(
            redis, event_id=event.id, zone_id=zone_id, user_id=1, quantity=4,
            total_price_cents=400, idempotency_key=str(uuid4()),
        )
    assert excinfo.value.feasible == [1, 2, 3, 5]
    assert MAX_TICKETS_PER_ORDER >= 5


# ─ 釋放與合併

async def test_release_merges_left_right_and_both(db, redis, zone_fixture) -> None:
    """三種合併都要走到:只併右鄰、只併左鄰、左右都併。

    落點不能交給配位決定,否則測不到想測的形狀。所以先在 DB 建三筆相鄰的 hold,
    重建出一個已知的起始狀態,再逐一釋放。
    """
    event, zone_id = zone_fixture["event"], zone_fixture["zone_id"]
    block = await db.scalar(
        select(SeatBlock).where(SeatBlock.zone_id == zone_id, SeatBlock.capacity == 12)
    )
    for start in (0, 2, 4):
        order = Order(
            user_id=zone_fixture["user"].id, event_id=event.id, zone_id=zone_id,
            quantity=2, total_price_cents=200, status=OrderStatus.PENDING,
            idempotency_key=uuid4(),
        )
        db.add(order)
        await db.flush()
        db.add(SeatHold(
            event_id=event.id, block_id=block.id, order_id=order.id,
            start_pos=start, length=2,
        ))
    await db.commit()
    await rebuild_zone_runs(db, redis, event_id=event.id, zone_id=zone_id)

    def in_block(runs: list[Run]) -> list[Run]:
        return [r for r in runs if r.block_id == block.id]

    assert in_block(await _redis_runs(redis, event.id, zone_id)) == [Run(block.id, 6, 6)]

    async def give_back(start: int, marker: str) -> list[Run]:
        assert await release_seats(
            redis, event_id=event.id, zone_id=zone_id, block_id=block.id,
            start_pos=start, length=2, marker=marker,
        )
        await _assert_indexes_agree(redis, event.id, zone_id)
        return in_block(await _redis_runs(redis, event.id, zone_id))

    # [4,6) 的右鄰是 [6,12) → 只併右。
    assert await give_back(4, "m-right") == [Run(block.id, 4, 8)]
    # [0,2) 兩側都沒有緊鄰的空段 → 獨立一段。
    assert await give_back(0, "m-none") == [Run(block.id, 0, 2), Run(block.id, 4, 8)]
    # [2,4) 左鄰 [0,2)、右鄰 [4,12) → 左右都併,回到完整一段。
    assert await give_back(2, "m-both") == [Run(block.id, 0, 12)]


async def test_release_is_idempotent_per_marker(db, redis, zone_fixture) -> None:
    event, zone_id = zone_fixture["event"], zone_fixture["zone_id"]
    reserved = await reserve_seats_and_enqueue(
        redis, event_id=event.id, zone_id=zone_id, user_id=1, quantity=2,
        total_price_cents=200, idempotency_key=str(uuid4()),
    )
    assert reserved is not None
    args = dict(
        event_id=event.id, zone_id=zone_id, block_id=reserved.block_id,
        start_pos=reserved.start_pos, length=reserved.length, marker="order:7",
    )
    assert await release_seats(redis, **args) is True
    assert await release_seats(redis, **args) is False, "重放必須是 no-op"
    assert int(await redis.get(_zone_available_key(event.id, zone_id))) == 48
    await _assert_indexes_agree(redis, event.id, zone_id)


# ─ 差異測試:Lua vs 純函式

async def test_lua_matches_the_pure_functions(db, redis, zone_fixture) -> None:
    """同一串隨機序列跑過 Redis+Lua 與 seating 的純函式,每步比對完整狀態。

    純函式有 53 個測試守著,所以它是 oracle。這是唯一能確信 Lua 的切分(0~2 段
    殘餘)與合併(boundary tags)跟參考實作一致的方法。
    """
    event, zone_id = zone_fixture["event"], zone_fixture["zone_id"]
    rng = random.Random(90210)
    pure = sorted(
        await _redis_runs(redis, event.id, zone_id),
        key=lambda r: (r.block_id, r.start),
    )
    live: list[tuple[int, int, int, str]] = []
    reserved_count = released_count = 0

    for step in range(120):
        if live and rng.random() < 0.35:
            block_id, start, length, marker = live.pop(rng.randrange(len(live)))
            assert await release_seats(
                redis, event_id=event.id, zone_id=zone_id, block_id=block_id,
                start_pos=start, length=length, marker=marker,
            )
            pure = release(pure, block_id=block_id, start=start, length=length)
            released_count += 1
        else:
            quantity = rng.randint(1, 4)
            try:
                reserved = await reserve_seats_and_enqueue(
                    redis, event_id=event.id, zone_id=zone_id, user_id=1,
                    quantity=quantity, total_price_cents=100 * quantity,
                    idempotency_key=str(uuid4()), rng=rng,
                )
            except NoSeatsAvailable:
                continue
            assert reserved is not None
            pure = occupy(
                pure,
                Placement(reserved.block_id, reserved.start_pos, reserved.length, 0.0),
            )
            live.append(
                (reserved.block_id, reserved.start_pos, reserved.length, f"m{step}")
            )
            reserved_count += 1

        assert await _redis_runs(redis, event.id, zone_id) == pure, f"step {step}"
        await _assert_indexes_agree(redis, event.id, zone_id)
        assert int(
            await redis.get(_zone_available_key(event.id, zone_id))
        ) == sum(r.length for r in pure), f"counter drift at step {step}"

    # 隨機測試會靜默退化(例如一路 NoSeatsAvailable 就什麼都沒比對到)。
    assert reserved_count >= 30, reserved_count
    assert released_count >= 15, released_count


async def test_no_orphan_survives_the_random_sequence(db, redis, zone_fixture) -> None:
    """硬約束在跨 Lua 之後仍然成立:不存在長度 1 的空段。

    (單人票取消可以製造孤兒,所以這裡只下單、不取消。)
    """
    event, zone_id = zone_fixture["event"], zone_fixture["zone_id"]
    rng = random.Random(555)
    while True:
        try:
            reserved = await reserve_seats_and_enqueue(
                redis, event_id=event.id, zone_id=zone_id, user_id=1,
                quantity=rng.randint(2, 4), total_price_cents=200,
                idempotency_key=str(uuid4()), rng=rng,
            )
        except NoSeatsAvailable:
            break
        assert reserved is not None
        for run in await _redis_runs(redis, event.id, zone_id):
            assert run.length != 1, f"出現孤兒段 {run}"
    assert NORMAL_POLICY.min_run == 2
