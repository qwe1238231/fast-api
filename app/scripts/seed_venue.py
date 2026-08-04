"""從宣告式描述建出座位圖(venue / zones / seat_blocks / seats)。

用法: python -m app.scripts.seed_venue [venue 名稱]

為什麼 seeding 值得認真寫:整個座位設計的前提是「`pos` 是稠密索引、`label` 是門牌,
兩者必須分開」。如果 seed 器只會產生 1,2,3,... 的連續門牌,那個前提從來沒被驗證過 ——
真實場館會跳過 4 與 13、會單雙號分邊,而那正是「用 label 判斷相鄰」會出錯的地方。
所以這裡提供三種 labeller,測試會用最刁鑽的那個。
"""
import asyncio
import itertools
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.seating import Seat, SeatBlock, Venue, Zone

#: quality_edge = quality_base × 這個比例。維持 block 之間的排序不被 block
#: 內部的落差蓋掉(若 edge 一律為 0,所有 block 的邊緣座位都同分)。
EDGE_RATIO = 0.5

#: 台灣/華語場館常見的忌諱號碼。
SUPERSTITIOUS_SKIPS = frozenset({4, 13, 14, 24, 34, 44})

#: 每個 block 一組門牌。輸入是「這一排被走道切出的各段座位數」。
Labeller = Callable[[Sequence[int]], list[list[str]]]


def sequential_labels(blocks: Sequence[int]) -> list[list[str]]:
    """1, 2, 3, ... 橫跨整排連號。最單純,也最不像真實場館。"""
    counter = itertools.count(1)
    return [[str(next(counter)) for _ in range(size)] for size in blocks]


def skipping_labels(skip: frozenset[int] = SUPERSTITIOUS_SKIPS) -> Labeller:
    """連號但跳過忌諱號碼 —— 於是門牌之差不再等於座位之差。"""

    def labeller(blocks: Sequence[int]) -> list[list[str]]:
        out: list[list[str]] = []
        number = 1
        for size in blocks:
            labels: list[str] = []
            while len(labels) < size:
                while number in skip:
                    number += 1
                labels.append(str(number))
                number += 1
            out.append(labels)
        return out

    return labeller


def odd_even_labels(blocks: Sequence[int]) -> list[list[str]]:
    """中線分邊:左側單號、右側雙號,號碼由中線往外遞增。

    這是最刁鑽的一種 —— 相鄰的兩個 pos 門牌相差 2,而中線兩側的 1 號與 2 號
    門牌相鄰但確實坐在一起。任何「用門牌判斷連續性」的程式碼在這裡都會壞。
    """
    total = sum(blocks)
    mid = total // 2
    numbers = [
        str(2 * (mid - pos) - 1) if pos < mid else str(2 * (pos - mid) + 2)
        for pos in range(total)
    ]
    out: list[list[str]] = []
    start = 0
    for size in blocks:
        out.append(numbers[start : start + size])
        start += size
    return out


@dataclass(frozen=True, slots=True)
class RowSpec:
    """一排。`blocks` 是走道切出的各段座位數 —— 每一段都是獨立的 SeatBlock。"""

    label: str
    blocks: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ZoneSpec:
    name: str
    display_order: int
    """越小越靠舞台。"""

    rows: tuple[RowSpec, ...]
    quality_front: float = 1.0
    """這一區最前排、最中央 block 的 quality_base。"""

    quality_back: float = 0.6
    """這一區最後排的 quality_base。"""

    side_penalty: float = 0.2
    """同一排裡,最外側 block 相對中央 block 的品質折扣。"""

    labeller: Labeller = sequential_labels


@dataclass(frozen=True, slots=True)
class VenueSpec:
    name: str
    zones: tuple[ZoneSpec, ...] = field(default_factory=tuple)

    @property
    def total_seats(self) -> int:
        return sum(
            size for zone in self.zones for row in zone.rows for size in row.blocks
        )


def _quality_base(
    zone: ZoneSpec, *, row_index: int, block_index: int, blocks: Sequence[int]
) -> float:
    """一個 block 的整體品質:排的遠近 × 水平偏離中線的程度。

    注意這只是 block **之間**的比較;block 內部由 `BlockGeometry` 用
    base/edge 線性內插。兩層要分開,否則會在 block 內部破壞單峰。
    """
    rows = max(1, len(zone.rows) - 1)
    depth = zone.quality_front + (zone.quality_back - zone.quality_front) * (
        row_index / rows
    )

    total = sum(blocks)
    row_center = (total - 1) / 2
    block_center = sum(blocks[:block_index]) + (blocks[block_index] - 1) / 2
    offset = abs(block_center - row_center) / max(row_center, 1e-9)

    return min(1.0, max(0.05, depth * (1 - zone.side_penalty * min(1.0, offset))))


async def seed_venue(db: AsyncSession, spec: VenueSpec) -> Venue:
    """建出整個座位圖。不 commit —— 交給呼叫端決定交易邊界。

    刻意不做 upsert:座位圖一旦有訂單掛上去就不該被 seed 器改寫,寧可失敗。
    """
    clash = await db.scalar(select(Venue.id).where(Venue.name == spec.name))
    if clash is not None:
        raise ValueError(
            f"venue {spec.name!r} 已存在(id={clash});seeding 不覆寫既有座位圖"
        )

    venue = Venue(name=spec.name)
    db.add(venue)
    await db.flush()

    for zone_spec in spec.zones:
        zone = Zone(
            venue_id=venue.id,
            name=zone_spec.name,
            display_order=zone_spec.display_order,
        )
        db.add(zone)
        await db.flush()

        pending: list[tuple[SeatBlock, list[str]]] = []
        for row_index, row in enumerate(zone_spec.rows):
            label_groups = zone_spec.labeller(row.blocks)
            for block_index, (size, labels) in enumerate(
                zip(row.blocks, label_groups, strict=True)
            ):
                base = _quality_base(
                    zone_spec,
                    row_index=row_index,
                    block_index=block_index,
                    blocks=row.blocks,
                )
                block = SeatBlock(
                    zone_id=zone.id,
                    row_label=row.label,
                    block_index=block_index,
                    capacity=size,
                    quality_base=base,
                    quality_edge=base * EDGE_RATIO,
                )
                db.add(block)
                pending.append((block, labels))

        await db.flush()          # 一次拿到整個 zone 的 block id,不是每個 block 一次
        for block, labels in pending:
            db.add_all(
                [
                    Seat(block_id=block.id, pos=pos, label=label)
                    for pos, label in enumerate(labels)
                ]
            )

    await db.flush()
    return venue


def _rows(prefix: str, count: int, blocks: tuple[int, ...]) -> tuple[RowSpec, ...]:
    return tuple(RowSpec(label=f"{prefix}{i + 1}", blocks=blocks) for i in range(count))


#: 一個三區的示範場館。三個 zone 刻意用三種不同的門牌規則。
DEMO_ARENA = VenueSpec(
    name="Demo Arena",
    zones=(
        ZoneSpec(
            name="搖滾區",
            display_order=0,
            rows=_rows("R", 10, (6, 12, 6)),
            quality_front=1.0,
            quality_back=0.85,
            labeller=odd_even_labels,
        ),
        ZoneSpec(
            name="看台A",
            display_order=1,
            rows=_rows("A", 15, (8, 16, 8)),
            quality_front=0.8,
            quality_back=0.55,
            labeller=skipping_labels(),
        ),
        ZoneSpec(
            name="看台B",
            display_order=2,
            rows=_rows("B", 20, (10, 20, 10)),
            quality_front=0.5,
            quality_back=0.25,
            labeller=sequential_labels,
        ),
    ),
)


async def main(venue_name: str | None = None) -> None:
    spec = DEMO_ARENA if venue_name is None else VenueSpec(name=venue_name)
    async with AsyncSessionLocal() as db:
        venue = await seed_venue(db, spec)
        await db.commit()

        rows = (
            await db.execute(
                select(
                    Zone.name,
                    func.count(SeatBlock.id.distinct()),
                    func.coalesce(func.sum(SeatBlock.capacity), 0),
                )
                .join(SeatBlock, SeatBlock.zone_id == Zone.id)
                .where(Zone.venue_id == venue.id)
                .group_by(Zone.name, Zone.display_order)
                .order_by(Zone.display_order)
            )
        ).all()

    print(f"venue {venue.name!r} (id={venue.id})")
    for name, blocks, seats in rows:
        print(f"  {name:<10} {blocks:>4} blocks  {seats:>6} seats")
    print(f"  {'total':<10} {'':>4}          {spec.total_seats:>6} seats")


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
    except ValueError as exc:      # 已存在的 venue —— 是預期的拒絕,不是 crash
        sys.exit(f"seed_venue: {exc}")
