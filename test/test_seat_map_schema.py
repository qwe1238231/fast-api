"""座位圖 schema 的資料庫層保證。

這個檔案測的不是 Python 邏輯,是 **Postgres 的約束**。重點是那條
`ex_seat_holds_no_overlap` —— 它讓「兩個人拿到同一張椅子」在資料庫層面物理上
不可能發生,不管配位 Lua 有沒有 bug、stream 有沒有重放。庫存超賣是退錢,現場兩人
同座是衝突,所以座位比數量更需要這道網,而網子本身值得有測試。
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.event import Event, EventStatus
from app.models.order import Order, OrderStatus
from app.models.seating import EventZonePrice, Seat, SeatBlock, SeatHold, Venue, Zone
from app.models.user import User

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def seat_map(db):
    """一個最小場館:1 venue / 2 zone / 每 zone 1 block(容量 10)/ 全部座位。"""
    venue = Venue(name="Seat Map Arena")
    db.add(venue)
    await db.flush()

    blocks: list[SeatBlock] = []
    for index, (name, order, base) in enumerate(
        [("搖滾區", 0, 1.0), ("看台A", 1, 0.7)]
    ):
        zone = Zone(venue_id=venue.id, name=name, display_order=order)
        db.add(zone)
        await db.flush()
        block = SeatBlock(
            zone_id=zone.id,
            row_label=chr(ord("A") + index),
            block_index=0,
            capacity=10,
            quality_base=base,
            quality_edge=base / 2,
        )
        db.add(block)
        await db.flush()
        db.add_all(
            [Seat(block_id=block.id, pos=pos, label=str(pos + 1)) for pos in range(10)]
        )
        blocks.append(block)

    now = datetime.now(timezone.utc)
    event = Event(
        name="Seated Concert",
        venue="Seat Map Arena",
        venue_id=venue.id,
        starts_at=now + timedelta(days=30),
        ends_at=now + timedelta(days=30, hours=3),
        sale_starts_at=now - timedelta(days=1),
        sale_ends_at=now + timedelta(days=1),
        total_seats=20,
        price_cents=1500,
        status=EventStatus.PUBLISHED,
    )
    user = User(username="seatbuyer", hashed_password="x")
    db.add_all([event, user])
    await db.flush()

    for block, price in zip(blocks, (5000, 2500)):
        db.add(
            EventZonePrice(
                event_id=event.id, zone_id=block.zone_id, price_cents=price
            )
        )
    await db.commit()
    return {"venue": venue, "event": event, "user": user, "blocks": blocks}


async def _order(db, seat_map, *, quantity: int, zone_id: int) -> Order:
    order = Order(
        user_id=seat_map["user"].id,
        event_id=seat_map["event"].id,
        zone_id=zone_id,
        quantity=quantity,
        total_price_cents=quantity * 5000,
        status=OrderStatus.PENDING,
        idempotency_key=uuid4(),
    )
    db.add(order)
    await db.flush()
    return order


async def _hold(db, seat_map, order, block, *, start_pos: int, length: int) -> SeatHold:
    hold = SeatHold(
        event_id=seat_map["event"].id,
        block_id=block.id,
        order_id=order.id,
        start_pos=start_pos,
        length=length,
    )
    db.add(hold)
    await db.flush()
    return hold


# ─ EXCLUDE:重疊不可能

@pytest.mark.parametrize(
    ("first", "second"),
    [
        ((0, 4), (2, 4)),   # 部分重疊
        ((0, 4), (0, 4)),   # 完全相同
        ((2, 4), (0, 6)),   # 被包含
        ((0, 10), (5, 1)),  # 單一座位落在既有區間內
    ],
)
async def test_overlapping_holds_are_impossible(db, seat_map, first, second) -> None:
    block = seat_map["blocks"][0]
    order_a = await _order(db, seat_map, quantity=first[1], zone_id=block.zone_id)
    await _hold(db, seat_map, order_a, block, start_pos=first[0], length=first[1])

    order_b = await _order(db, seat_map, quantity=second[1], zone_id=block.zone_id)
    with pytest.raises(IntegrityError):
        await _hold(db, seat_map, order_b, block, start_pos=second[0], length=second[1])


async def test_touching_holds_are_allowed(db, seat_map) -> None:
    """[0,4) 與 [4,8) 只是相鄰不是重疊 —— 半開區間必須讓這種情況通過。

    這是 int4range 用 `[)` 而不是 `[]` 的直接後果;搞錯的話整個場館只能賣一半。
    """
    block = seat_map["blocks"][0]
    for start in (0, 4, 8):
        order = await _order(db, seat_map, quantity=2, zone_id=block.zone_id)
        await _hold(db, seat_map, order, block, start_pos=start, length=2)
    await db.commit()
    assert len((await db.scalars(select(SeatHold))).all()) == 3


async def test_same_interval_in_a_different_block_is_allowed(db, seat_map) -> None:
    """pos 是 block 內的座標,不同 block 的 [0,4) 是不同的椅子。"""
    block_a, block_b = seat_map["blocks"]
    for block in (block_a, block_b):
        order = await _order(db, seat_map, quantity=4, zone_id=block.zone_id)
        await _hold(db, seat_map, order, block, start_pos=0, length=4)
    await db.commit()
    assert len((await db.scalars(select(SeatHold))).all()) == 2


async def test_exclude_constraint_can_be_deferred_for_compaction(db, seat_map) -> None:
    """整批滑動 pending hold 時,中間狀態會短暫重疊。

    DEFERRABLE 讓 compaction 可以做任意排列而不必自己算出一條無衝突的搬移順序 ——
    只要 txn 結束時佈局是合法的就行。
    """
    block = seat_map["blocks"][0]
    holds = []
    for start in (0, 2, 4):
        order = await _order(db, seat_map, quantity=2, zone_id=block.zone_id)
        holds.append(
            await _hold(db, seat_map, order, block, start_pos=start, length=2)
        )
    await db.commit()

    await db.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    # 整體右移 4:第一步 0→4 立刻與既有的 [4,6) 重疊,IMMEDIATE 下會當場失敗。
    for hold in holds:
        hold.start_pos += 4
    await db.commit()

    starts = sorted((await db.scalars(select(SeatHold.start_pos))).all())
    assert starts == [4, 6, 8]


# ─ 其餘約束

async def test_one_hold_per_order(db, seat_map) -> None:
    block_a, block_b = seat_map["blocks"]
    order = await _order(db, seat_map, quantity=2, zone_id=block_a.zone_id)
    await _hold(db, seat_map, order, block_a, start_pos=0, length=2)
    with pytest.raises(IntegrityError):
        await _hold(db, seat_map, order, block_b, start_pos=0, length=2)


async def test_zero_length_hold_is_rejected(db, seat_map) -> None:
    block = seat_map["blocks"][0]
    order = await _order(db, seat_map, quantity=1, zone_id=block.zone_id)
    with pytest.raises(IntegrityError):
        await _hold(db, seat_map, order, block, start_pos=0, length=0)


@pytest.mark.parametrize(
    ("base", "edge"),
    [(1.0, 1.5), (0.5, 0.8), (1.5, 0.5), (1.0, -0.1)],
)
async def test_block_quality_must_stay_in_unit_range(db, seat_map, base, edge) -> None:
    """quality 必須是 0 <= edge <= base <= 1。

    score 拿 quality 跟 cut_cost 相減,而 cut_cost 的單位是「座位數」;品質跑出
    [0, 1] 的話 λ 就失去意義,配位的取捨會靜默偏掉。DB 擋在最外層。
    """
    zone_id = seat_map["blocks"][0].zone_id
    db.add(
        SeatBlock(
            zone_id=zone_id,
            row_label="Z",
            block_index=0,
            capacity=10,
            quality_base=base,
            quality_edge=edge,
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


# 兩個唯一約束分成兩個測試:一次 flush 失敗之後 session 就進了需要 rollback 的
# 狀態,在同一個 session 裡接著操作 ORM 物件會踩到 expired-attribute 的延遲 IO。
async def test_seat_pos_is_unique_per_block(db, seat_map) -> None:
    block_id = seat_map["blocks"][0].id
    db.add(Seat(block_id=block_id, pos=0, label="999"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_seat_label_is_unique_per_block(db, seat_map) -> None:
    block_id = seat_map["blocks"][0].id
    db.add(Seat(block_id=block_id, pos=99, label="1"))
    with pytest.raises(IntegrityError):
        await db.flush()


# ─ 越界:區間不得超出 block 的容量

@pytest.mark.parametrize(
    ("start", "length"),
    [
        (8, 4),    # 尾巴越過上界
        (10, 2),   # 整段在 block 外
        (0, 11),   # 比整個 block 還長
    ],
)
async def test_a_hold_beyond_the_block_is_impossible(db, seat_map, start, length) -> None:
    """EXCLUDE 擋的是「兩個人同一張椅子」;這條擋另一種損壞 ——「賣出一張根本沒有
    那個位子的票」。

    capacity 在另一張表,所以 CHECK 表達不了。用「生成欄位 last_pos + 指向
    seats(block_id, pos) 的複合外鍵」純宣告地表達:區間最後一個 pos 必須真的存在。
    """
    block = seat_map["blocks"][0]           # capacity = 10,pos 0..9
    order = await _order(db, seat_map, quantity=length, zone_id=block.zone_id)
    with pytest.raises(IntegrityError):
        await _hold(db, seat_map, order, block, start_pos=start, length=length)


async def test_a_hold_ending_on_the_last_seat_is_allowed(db, seat_map) -> None:
    """邊界值:剛好用到最後一個座位必須通過 —— 差一格的檢查會讓每個 block 少賣一席。"""
    block = seat_map["blocks"][0]
    order = await _order(db, seat_map, quantity=2, zone_id=block.zone_id)
    await _hold(db, seat_map, order, block, start_pos=8, length=2)   # pos 8,9
    await db.commit()
    assert await db.scalar(select(SeatHold.last_pos)) == 9


async def test_last_pos_is_generated_not_writable(db, seat_map) -> None:
    """生成欄位:寫進去的值會被忽略,由 DB 自己算 —— 否則它就只是另一個會漂移的
    反正規化欄位,而那正是這個設計要避開的東西。"""
    block = seat_map["blocks"][0]
    order = await _order(db, seat_map, quantity=2, zone_id=block.zone_id)
    hold = SeatHold(
        event_id=seat_map["event"].id, block_id=block.id, order_id=order.id,
        start_pos=2, length=2,
    )
    db.add(hold)
    await db.commit()
    assert await db.scalar(
        select(SeatHold.last_pos).where(SeatHold.id == hold.id)
    ) == 3
