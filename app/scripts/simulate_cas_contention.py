"""「讀 runs → 在應用層算 → CAS」到底可不可行?

配位有兩條路可走:

  (A) 整個配位邏輯搬進 Redis Lua,原子完成。要移植演算法 + 做差異測試。
  (B) 應用層 HGETALL → 用現成的 `allocate()` 算 → 一個很短的 Lua 做 CAS
      (「這個區間仍完全落在某空段內就佔用,否則回 RETRY」)。不必移植,
      只有一份實作、不會漂移。

(B) 明顯便宜。它的風險在於 `allocate()` 是**決定性**的(刻意加了全序 tiebreak,
為了讓結果不依賴迭代順序),所以同時讀到同一份快照的請求會算出**同一個**
placement → CAS 幾乎全部互撞。決定性在單機測試裡是優點,在樂觀鎖下可能變成
thundering herd。

這個腳本量測那個猜測,並量測它的解藥(從 top-K 候選隨機挑)要付多少品質代價。

關鍵參數是 N —— 「同時看到同一份快照的請求數」。它不是總 QPS,而是
QPS × (讀→算→CAS 的時間窗)。1000 QPS 配 2ms 窗 → N ≈ 2;10000 QPS 配 5ms
窗 → N ≈ 50。所以下面掃 N 而不是取一個大數字。

    python -m app.scripts.simulate_cas_contention
"""
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.scripts.seed_venue import DEMO_ARENA, EDGE_RATIO, ZoneSpec
from app.services.seating import (
    NORMAL_POLICY,
    BlockGeometry,
    Placement,
    Policy,
    Run,
    allocate,
    legal_anchors,
    occupy,
)

TYPICAL_DEMAND: Mapping[int, float] = {1: 0.05, 2: 0.55, 3: 0.15, 4: 0.25}

BATCH_SIZES = (2, 5, 10, 25, 50, 100)
TOP_K = (1, 4, 16, 64)
SEEDS = tuple(range(4242, 4242 + 12))


def _geometry_from_zone(zone: ZoneSpec, zone_index: int) -> dict[int, BlockGeometry]:
    """把一個 ZoneSpec 攤平成 BlockGeometry —— 配位一律限制在單一 zone 內
    (票價已經編碼了跨 zone 的品質差異)。"""
    from app.scripts.seed_venue import _quality_base

    geometry: dict[int, BlockGeometry] = {}
    block_id = zone_index * 10_000
    for row_index, row in enumerate(zone.rows):
        for block_index, size in enumerate(row.blocks):
            base = _quality_base(
                zone, row_index=row_index, block_index=block_index, blocks=row.blocks
            )
            geometry[block_id] = BlockGeometry.calibrated(
                block_id, size, base=base, edge=base * EDGE_RATIO
            )
            block_id += 1
    return geometry


def _fits(runs: Sequence[Run], placement: Placement) -> bool:
    end = placement.start + placement.length
    return any(
        run.block_id == placement.block_id
        and run.start <= placement.start
        and end <= run.start + run.length
        for run in runs
    )


def _prefill(
    runs: list[Run],
    geometry: Mapping[int, BlockGeometry],
    rng: random.Random,
    *,
    sold_ratio: float,
    policy: Policy,
) -> list[Run]:
    """先賣掉一部分,製造真實的碎片分佈。"""
    capacity = sum(run.length for run in runs)
    target = capacity * sold_ratio
    sold = 0
    quantities, weights = list(TYPICAL_DEMAND), list(TYPICAL_DEMAND.values())
    while sold < target:
        quantity = rng.choices(quantities, weights=weights, k=1)[0]
        placed = allocate(runs, quantity, geometry, policy)
        if placed is None:
            break
        runs = occupy(runs, placed)
        sold += quantity
    return runs


@dataclass(frozen=True, slots=True)
class Round:
    attempts: int
    successes: int
    quality_gap: float
    """選中的 placement 相對於當輪最佳 score 的平均落差(座位品質單位)。"""


def one_round(
    runs: Sequence[Run],
    geometry: Mapping[int, BlockGeometry],
    rng: random.Random,
    *,
    batch: int,
    top_k: int,
    policy: Policy,
) -> Round:
    """N 個請求讀到**同一份快照**,各自算,然後序列化 CAS。"""
    quantities, weights = list(TYPICAL_DEMAND), list(TYPICAL_DEMAND.values())
    wanted = rng.choices(quantities, weights=weights, k=batch)

    # 同一份快照 + 同一個張數 → 同一份候選清單。快取住,也更忠於現實。
    anchors: dict[int, list[Placement]] = {}
    for quantity in set(wanted):
        anchors[quantity] = legal_anchors(runs, quantity, geometry, policy)

    chosen: list[Placement] = []
    gaps: list[float] = []
    for quantity in wanted:
        options = anchors[quantity]
        if not options:
            continue
        pick = options[rng.randrange(min(top_k, len(options)))]
        chosen.append(pick)
        gaps.append(options[0].score - pick.score)

    live = list(runs)
    successes = 0
    rng.shuffle(chosen)                        # CAS 到達順序是隨機的
    for placement in chosen:
        if _fits(live, placement):
            live = occupy(live, placement)
            successes += 1
    return Round(
        attempts=len(chosen),
        successes=successes,
        quality_gap=sum(gaps) / len(gaps) if gaps else 0.0,
    )


def sweep(*, sold_ratio: float, policy: Policy = NORMAL_POLICY) -> None:
    zone = DEMO_ARENA.zones[1]                 # 看台A:45 blocks / 480 座
    geometry = _geometry_from_zone(zone, zone_index=1)
    base_runs = [
        Run(block_id, 0, geo.capacity) for block_id, geo in geometry.items()
    ]

    print(f"\n=== zone={zone.name} 已售 {sold_ratio:.0%} / {len(base_runs)} blocks ===")
    print(f"{'N (同時看到同一快照)':<24}" + "".join(f"{f'K={k}':>12}" for k in TOP_K))

    for batch in BATCH_SIZES:
        cells: list[str] = []
        for top_k in TOP_K:
            attempts = successes = 0
            for seed in SEEDS:
                rng = random.Random(seed)
                runs = _prefill(
                    list(base_runs), geometry, rng,
                    sold_ratio=sold_ratio, policy=policy,
                )
                result = one_round(
                    runs, geometry, rng, batch=batch, top_k=top_k, policy=policy
                )
                attempts += result.attempts
                successes += result.successes
            rate = successes / attempts if attempts else 0.0
            cells.append(f"{rate:>11.1%}")
        print(f"{f'N={batch}':<24}" + "".join(cells))


def quality_cost() -> None:
    """隨機化的代價:K 越大,選到的座位比最佳差多少。"""
    zone = DEMO_ARENA.zones[1]
    geometry = _geometry_from_zone(zone, zone_index=1)
    base_runs = [Run(bid, 0, geo.capacity) for bid, geo in geometry.items()]

    print("\n=== 隨機化的品質代價(已售 50%)===")
    print(f"{'top-K':<10}{'平均品質落差(座位)':>22}")
    for top_k in TOP_K:
        gaps: list[float] = []
        for seed in SEEDS:
            rng = random.Random(seed)
            runs = _prefill(
                list(base_runs), geometry, rng,
                sold_ratio=0.5, policy=NORMAL_POLICY,
            )
            gaps.append(
                one_round(
                    runs, geometry, rng, batch=50, top_k=top_k, policy=NORMAL_POLICY
                ).quality_gap
            )
        print(f"{f'K={top_k}':<10}{sum(gaps) / len(gaps):>22.3f}")


def compute_cost() -> float:
    """量測 legal_anchors 在一個真實 zone 上的耗時 —— 它決定 CAS 的時間窗 T,
    而 T 決定「同時看到同一快照的請求數」N = λ × T。回傳毫秒。"""
    import time

    zone = DEMO_ARENA.zones[1]
    geometry = _geometry_from_zone(zone, zone_index=1)
    runs = _prefill(
        [Run(bid, 0, geo.capacity) for bid, geo in geometry.items()],
        geometry,
        random.Random(1),
        sold_ratio=0.5,
        policy=NORMAL_POLICY,
    )

    reps = 2000
    start = time.perf_counter()
    for _ in range(reps):
        legal_anchors(runs, 2, geometry, NORMAL_POLICY)
    elapsed_ms = (time.perf_counter() - start) / reps * 1000

    print(f"\n=== 計算成本 ===")
    print(f"legal_anchors({len(runs)} 個空段)  {elapsed_ms:.3f} ms/次")
    return elapsed_ms


def closed_loop(*, arrival_per_second: int, window_ms: float) -> None:
    """閉環:失敗的請求立刻重試,於是重試本身推高並發 —— 樂觀鎖的正回饋。

    單一輪的成功率看起來還行,不代表系統穩定。這裡跑到 zone 售完為止,量測
    「每筆成交要幾次 CAS」與「最壞情況要重試幾輪」。
    """
    zone = DEMO_ARENA.zones[2]                 # 看台B:60 blocks / 800 座
    geometry = _geometry_from_zone(zone, zone_index=2)
    per_step = arrival_per_second * window_ms / 1000

    print(
        f"\n=== 閉環 λ={arrival_per_second}/秒 T={window_ms}ms "
        f"(每步新到 {per_step:.2f} 筆)==="
    )
    print(f"{'top-K':<8}{'放大倍數':>12}{'p95 重試輪數':>16}{'最大待重試':>14}")

    for top_k in TOP_K:
        amplifications: list[float] = []
        p95s: list[int] = []
        peak_backlog = 0
        for seed in SEEDS:
            rng = random.Random(seed)
            runs = [Run(bid, 0, geo.capacity) for bid, geo in geometry.items()]
            quantities, weights = list(TYPICAL_DEMAND), list(TYPICAL_DEMAND.values())

            pending: list[int] = []            # 每個元素 = 已重試幾輪
            attempts = successes = 0
            retries_at_success: list[int] = []
            fractional = 0.0

            for _ in range(4000):
                fractional += per_step
                arrivals = int(fractional)
                fractional -= arrivals
                batch = pending + [0] * arrivals
                if not batch:
                    continue

                anchors: dict[int, list[Placement]] = {}
                picks: list[tuple[int, Placement]] = []
                for retried in batch:
                    quantity = rng.choices(quantities, weights=weights, k=1)[0]
                    if quantity not in anchors:
                        anchors[quantity] = legal_anchors(
                            runs, quantity, geometry, NORMAL_POLICY
                        )
                    options = anchors[quantity]
                    if not options:
                        continue                # 這個張數配不出來(NO_FIT),不是衝突
                    picks.append(
                        (retried, options[rng.randrange(min(top_k, len(options)))])
                    )

                if not picks:
                    break                       # 售完
                rng.shuffle(picks)
                pending = []
                for retried, placement in picks:
                    attempts += 1
                    if _fits(runs, placement):
                        runs = occupy(runs, placement)
                        successes += 1
                        retries_at_success.append(retried)
                    else:
                        pending.append(retried + 1)
                peak_backlog = max(peak_backlog, len(pending))

            if successes:
                amplifications.append(attempts / successes)
                retries_at_success.sort()
                p95s.append(retries_at_success[int(len(retries_at_success) * 0.95)])

        print(
            f"{f'K={top_k}':<8}"
            f"{sum(amplifications) / len(amplifications):>12.2f}"
            f"{sum(p95s) / len(p95s):>16.1f}"
            f"{peak_backlog:>14}"
        )


def main() -> None:
    print(f"{len(SEEDS)} seeds;表格數字 = 第一輪 CAS 成功率(越低越慘)")
    for sold_ratio in (0.0, 0.5, 0.9):
        sweep(sold_ratio=sold_ratio)
    quality_cost()
    window_ms = compute_cost() + 1.0           # + 兩趟 Redis round-trip 的粗估
    for arrival in (170, 500, 2000):
        closed_loop(arrival_per_second=arrival, window_ms=window_ms)


if __name__ == "__main__":
    main()
