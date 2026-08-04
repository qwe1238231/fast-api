"""座位配位的裝箱效率模擬。

回答的是「mid_cut_guard 該設多少」「λ 設多少」這類**沒有正確答案、只有取捨**的
問題 —— 單元測試證明不了它們,只能在一個假設的需求分布下跑出數字來比較。

這不是測試,它沒有 pass/fail。但 test/test_seating.py 會引用同一個 `simulate()`
做寬鬆的回歸下限,防止未來某次改動讓裝箱效率崩掉而沒有人發現。

    python -m app.scripts.simulate_seating
"""
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.services.seating import (
    ENDGAME_POLICY,
    NORMAL_POLICY,
    BlockGeometry,
    Placement,
    Policy,
    Run,
    allocate,
    occupy,
    release,
)

#: 搶票的典型張數分布:單人罕見,兩人為主。
TYPICAL_DEMAND: Mapping[int, float] = {1: 0.05, 2: 0.55, 3: 0.15, 4: 0.25}

#: 沒有單人票的分布 —— 用來驗證「孤兒只可能由單人票取消產生」。
NO_SINGLES_DEMAND: Mapping[int, float] = {2: 0.60, 3: 0.15, 4: 0.25}


@dataclass(frozen=True, slots=True)
class SimResult:
    total_seats: int
    remaining: int
    orphans: int
    """長度 1 的空段數量。"""

    unservable_ratio: float
    """空位中,位於「長度 < 最大團體」空段的比例 —— 大團體買不到的那部分。"""

    no_fit: Mapping[int, int]

    @property
    def sold_ratio(self) -> float:
        return (self.total_seats - self.remaining) / self.total_seats


def typical_venue(blocks: int = 30) -> list[BlockGeometry]:
    """一個走道把每排切成數段的看台:容量參差、品質隨 block 遞減。"""
    capacities = (18, 20, 21, 19, 22)
    return [
        BlockGeometry.calibrated(
            block_id=i,
            capacity=capacities[i % len(capacities)],
            base=1.0 - 0.02 * i,
        )
        for i in range(blocks)
    ]


def simulate(
    blocks: Sequence[BlockGeometry],
    demand: Mapping[int, float],
    policy: Policy = NORMAL_POLICY,
    *,
    cancel_rate: float = 0.0,
    rounds: int = 4000,
    seed: int = 20260803,
) -> SimResult:
    """跑一段訂單流(含取消),回傳最終的裝箱指標。"""
    rng = random.Random(seed)
    geometry = {block.block_id: block for block in blocks}
    runs = [Run(block.block_id, 0, block.capacity) for block in blocks]
    live: list[Placement] = []
    no_fit: Counter[int] = Counter()
    quantities, weights = list(demand), list(demand.values())

    for _ in range(rounds):
        if live and rng.random() < cancel_rate:
            victim = live.pop(rng.randrange(len(live)))
            runs = release(
                runs,
                block_id=victim.block_id,
                start=victim.start,
                length=victim.length,
            )
            continue

        quantity = rng.choices(quantities, weights=weights, k=1)[0]
        placed = allocate(runs, quantity, geometry, policy)
        if placed is None:
            no_fit[quantity] += 1
            continue
        runs = occupy(runs, placed)
        live.append(placed)

    remaining = sum(run.length for run in runs)
    biggest_group = max(demand)
    stranded = sum(run.length for run in runs if run.length < biggest_group)
    return SimResult(
        total_seats=sum(block.capacity for block in blocks),
        remaining=remaining,
        orphans=sum(1 for run in runs if run.length == 1),
        unservable_ratio=stranded / remaining if remaining else 0.0,
        no_fit=dict(sorted(no_fit.items())),
    )


#: 單一 seed 的 1% 差異是雜訊。任何結論都必須跨 seed 才算數。
SEEDS = tuple(range(20260803, 20260803 + 24))


@dataclass(frozen=True, slots=True)
class Aggregate:
    """跨 seed 的平均與最差值。"""

    sold_ratio: float
    worst_sold_ratio: float
    remaining: float
    orphans: float
    unservable_ratio: float


def aggregate(
    blocks: Sequence[BlockGeometry],
    demand: Mapping[int, float],
    policy: Policy = NORMAL_POLICY,
    *,
    cancel_rate: float = 0.0,
    seeds: Sequence[int] = SEEDS,
) -> Aggregate:
    results = [
        simulate(blocks, demand, policy, cancel_rate=cancel_rate, seed=seed)
        for seed in seeds
    ]
    n = len(results)
    return Aggregate(
        sold_ratio=sum(r.sold_ratio for r in results) / n,
        worst_sold_ratio=min(r.sold_ratio for r in results),
        remaining=sum(r.remaining for r in results) / n,
        orphans=sum(r.orphans for r in results) / n,
        unservable_ratio=sum(r.unservable_ratio for r in results) / n,
    )


_HEADER = (
    f"{'policy':<22}{'售出率':>10}{'最差':>9}{'剩餘':>8}"
    f"{'孤兒段':>9}{'大團體買不到':>14}"
)


def _row(label: str, agg: Aggregate) -> str:
    return (
        f"{label:<22}"
        f"{agg.sold_ratio:>10.2%}"
        f"{agg.worst_sold_ratio:>9.2%}"
        f"{agg.remaining:>8.1f}"
        f"{agg.orphans:>9.2f}"
        f"{agg.unservable_ratio:>14.1%}"
    )


def main() -> None:
    blocks = typical_venue()
    print(f"{len(SEEDS)} seeds × {sum(b.capacity for b in blocks)} 座")

    for cancel_rate in (0.0, 0.3):
        print(f"\n=== 取消率 {cancel_rate:.0%} / demand={TYPICAL_DEMAND} ===")
        print(_HEADER)
        for guard in (2, 3, 4, 6):
            print(
                _row(
                    f"mid_cut_guard={guard}",
                    aggregate(
                        blocks,
                        TYPICAL_DEMAND,
                        Policy(mid_cut_guard=guard),
                        cancel_rate=cancel_rate,
                    ),
                )
            )
        print(
            _row(
                "ENDGAME 全程",
                aggregate(
                    blocks, TYPICAL_DEMAND, ENDGAME_POLICY, cancel_rate=cancel_rate
                ),
            )
        )

    print("\n=== λ 的取捨(取消率 30%)===")
    print(_HEADER)
    for weight in (0.0, 0.5, 1.5, 4.0):
        print(
            _row(
                f"λ={weight}",
                aggregate(
                    blocks,
                    TYPICAL_DEMAND,
                    Policy(fragmentation_weight=weight),
                    cancel_rate=0.3,
                ),
            )
        )

    print("\n=== 孤兒的唯一來源(取消率 30%)===")
    print(_HEADER)
    print(_row("有單人票需求", aggregate(blocks, TYPICAL_DEMAND, cancel_rate=0.3)))
    print(_row("無單人票需求", aggregate(blocks, NO_SINGLES_DEMAND, cancel_rate=0.3)))


if __name__ == "__main__":
    main()
