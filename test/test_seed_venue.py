"""座位圖 seeding 的測試。

重點不是「有沒有建出列」,是**建出來的佈局能不能揭露設計裡的假設**:
`pos` 稠密、`label` 可以跳號、block 是走道切出的段。最後一個測試把 seed 出來的
資料直接餵進配位演算法 —— 那是 B0 與演算法層之間唯一的接縫。
"""
import pytest
from sqlalchemy import func, select

from app.models.seating import Seat, SeatBlock, Venue, Zone
from app.scripts.seed_venue import (
    DEMO_ARENA,
    EDGE_RATIO,
    SUPERSTITIOUS_SKIPS,
    RowSpec,
    VenueSpec,
    ZoneSpec,
    odd_even_labels,
    seed_venue,
    sequential_labels,
    skipping_labels,
)
from app.services.seating import BlockGeometry, Run, allocate, feasible_quantities

# ─ labeller:純函式,不碰 DB

def test_sequential_labels_run_across_the_whole_row() -> None:
    assert sequential_labels((3, 4)) == [["1", "2", "3"], ["4", "5", "6", "7"]]


def test_skipping_labels_omit_the_unlucky_numbers() -> None:
    labels = [label for group in skipping_labels()((20,)) for label in group]
    assert not (set(labels) & {str(n) for n in SUPERSTITIOUS_SKIPS})
    assert len(labels) == 20
    assert len(set(labels)) == 20


def test_skipping_labels_break_label_based_adjacency() -> None:
    """3 號與 5 號門牌不連號,但它們是相鄰的兩個座位。

    這就是 pos 與 label 必須分開的理由:用門牌判斷連續性會把這兩個位子判成
    不相鄰,於是一張兩人票配不進明明坐得下的地方。
    """
    labels = skipping_labels()((6,))[0]
    assert labels == ["1", "2", "3", "5", "6", "7"]
    assert int(labels[3]) - int(labels[2]) == 2      # pos 差 1,門牌差 2


def test_odd_even_labels_split_at_the_centre_line() -> None:
    left, right = odd_even_labels((4, 4))
    assert left == ["7", "5", "3", "1"]              # 左側單號,往中線遞減
    assert right == ["2", "4", "6", "8"]             # 右側雙號,往外遞增


@pytest.mark.parametrize("blocks", [(1,), (5,), (6, 12, 6), (7, 13), (3, 4, 5)])
def test_every_labeller_produces_unique_labels_of_the_right_shape(blocks) -> None:
    for labeller in (sequential_labels, skipping_labels(), odd_even_labels):
        groups = labeller(blocks)
        assert [len(group) for group in groups] == list(blocks)
        flat = [label for group in groups for label in group]
        assert len(set(flat)) == len(flat), labeller


# ─ seeding

@pytest.mark.asyncio
async def test_seed_demo_arena_shape(db) -> None:
    venue = await seed_venue(db, DEMO_ARENA)
    await db.commit()

    zones = (
        await db.scalars(
            select(Zone).where(Zone.venue_id == venue.id).order_by(Zone.display_order)
        )
    ).all()
    assert [zone.name for zone in zones] == ["搖滾區", "看台A", "看台B"]

    total = await db.scalar(
        select(func.coalesce(func.sum(SeatBlock.capacity), 0))
        .join(Zone, Zone.id == SeatBlock.zone_id)
        .where(Zone.venue_id == venue.id)
    )
    seats = await db.scalar(
        select(func.count(Seat.id))
        .join(SeatBlock, SeatBlock.id == Seat.block_id)
        .join(Zone, Zone.id == SeatBlock.zone_id)
        .where(Zone.venue_id == venue.id)
    )
    assert total == DEMO_ARENA.total_seats
    assert seats == DEMO_ARENA.total_seats, "capacity 與實際建出的座位數必須一致"


@pytest.mark.asyncio
async def test_seat_positions_are_dense_from_zero(db) -> None:
    """每個 block 的 pos 必須是 0..capacity-1,不能有洞。

    有洞的話 free-run 結構就會憑空多出「空段」,而那些位置其實不存在。
    """
    await seed_venue(db, DEMO_ARENA)
    await db.commit()

    blocks = (await db.scalars(select(SeatBlock))).all()
    assert blocks
    for block in blocks:
        positions = sorted(
            (await db.scalars(select(Seat.pos).where(Seat.block_id == block.id))).all()
        )
        assert positions == list(range(block.capacity)), block.id


@pytest.mark.asyncio
async def test_quality_is_calibrated_and_ordered(db) -> None:
    await seed_venue(db, DEMO_ARENA)
    await db.commit()

    blocks = (await db.scalars(select(SeatBlock))).all()
    for block in blocks:
        assert 0 <= block.quality_edge <= block.quality_base <= 1
        assert block.quality_edge == pytest.approx(block.quality_base * EDGE_RATIO)

    zone_id = await db.scalar(
        select(Zone.id).where(Zone.name == "看台B")
    )
    front = await db.scalar(
        select(SeatBlock.quality_base).where(
            SeatBlock.zone_id == zone_id, SeatBlock.row_label == "B1",
            SeatBlock.block_index == 1,
        )
    )
    back = await db.scalar(
        select(SeatBlock.quality_base).where(
            SeatBlock.zone_id == zone_id, SeatBlock.row_label == "B20",
            SeatBlock.block_index == 1,
        )
    )
    side = await db.scalar(
        select(SeatBlock.quality_base).where(
            SeatBlock.zone_id == zone_id, SeatBlock.row_label == "B1",
            SeatBlock.block_index == 0,
        )
    )
    assert front > back, "前排必須優於後排"
    assert front > side, "同一排的中央 block 必須優於外側"


@pytest.mark.asyncio
async def test_seeding_the_same_venue_twice_is_refused(db) -> None:
    """座位圖一旦有訂單掛上去就不該被 seed 器改寫,寧可失敗。"""
    await seed_venue(db, VenueSpec(name="Once Only"))
    await db.commit()
    with pytest.raises(ValueError, match="已存在"):
        await seed_venue(db, VenueSpec(name="Once Only"))


@pytest.mark.asyncio
async def test_zone_with_no_rows_creates_a_zone_but_no_blocks(db) -> None:
    spec = VenueSpec(
        name="Empty Zone Venue",
        zones=(ZoneSpec(name="尚未配置", display_order=0, rows=()),),
    )
    venue = await seed_venue(db, spec)
    await db.commit()
    assert await db.scalar(
        select(func.count(Zone.id)).where(Zone.venue_id == venue.id)
    ) == 1
    assert await db.scalar(select(func.count(SeatBlock.id))) == 0


# ─ 與演算法層的接縫

@pytest.mark.asyncio
async def test_seeded_blocks_drive_the_allocator(db) -> None:
    """從 DB 讀出 block → 建 BlockGeometry → 配位。

    這是 seeding 唯一的驗收標準:能不能真的餵給演算法。順便驗證 seed 出來的
    quality 通過 BlockGeometry 的 decay >= 0 守衛(單峰性)。
    """
    spec = VenueSpec(
        name="Allocator Feed",
        zones=(
            ZoneSpec(
                name="測試區",
                display_order=0,
                rows=(RowSpec("A", (6, 12, 6)), RowSpec("B", (6, 12, 6))),
                labeller=odd_even_labels,
            ),
        ),
    )
    await seed_venue(db, spec)
    await db.commit()

    zone_id = await db.scalar(select(Zone.id).where(Zone.name == "測試區"))
    blocks = (
        await db.scalars(select(SeatBlock).where(SeatBlock.zone_id == zone_id))
    ).all()

    geometry = {
        block.id: BlockGeometry.calibrated(
            block.id,
            block.capacity,
            base=block.quality_base,
            edge=block.quality_edge,
        )
        for block in blocks
    }
    runs = [Run(block.id, 0, block.capacity) for block in blocks]

    assert feasible_quantities(runs, geometry, max_order=6) == [1, 2, 3, 4, 5, 6]

    placed = allocate(runs, 4, geometry)
    assert placed is not None
    # 最好的位子必須落在容量 12 的中央 block(側邊 block 有 side_penalty 折扣)
    assert geometry[placed.block_id].capacity == 12

    # 配出來的 4 個座位在 DB 裡真的存在,而且門牌不必連號。
    labels = (
        await db.scalars(
            select(Seat.label)
            .where(
                Seat.block_id == placed.block_id,
                Seat.pos >= placed.start,
                Seat.pos < placed.start + placed.length,
            )
            .order_by(Seat.pos)
        )
    ).all()
    assert len(labels) == 4
