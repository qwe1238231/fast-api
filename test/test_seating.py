"""座位配位演算法的不變式測試。

`app.services.seating` 是純函式,所以這裡不需要 DB / Redis fixture。
最後兩組測試(暴力 oracle、λ 語意)是這個檔案存在的主要理由:
它們擋的是「O(1) 優化其實漏掉更好解」與「旋鈕語意反了」這兩類
在生產環境只會表現成「座位品質有點怪」、永遠沒人回報的 bug。
"""
import random

import pytest

from app.scripts.simulate_seating import (
    NO_SINGLES_DEMAND,
    TYPICAL_DEMAND,
    simulate,
    typical_venue,
)
from app.services.seating import (
    ENDGAME_POLICY,
    NORMAL_POLICY,
    BlockGeometry,
    Placement,
    Policy,
    Run,
    allocate,
    candidates,
    cut_cost,
    feasible_quantities,
    legal_anchors,
    occupy,
    placement_at,
    release,
)

MAX_ORDER = 6


def _free_runs(block_id: int, capacity: int, occupied: set[int]) -> list[Run]:
    """從佔用集合反推極大連續空段 —— 測試裡的 free-run 參考實作。"""
    runs: list[Run] = []
    start: int | None = None
    for pos in range(capacity + 1):
        is_free = pos < capacity and pos not in occupied
        if is_free and start is None:
            start = pos
        elif not is_free and start is not None:
            runs.append(Run(block_id, start, pos - start))
            start = None
    return runs


def _legal_offsets(run: Run, quantity: int, policy: Policy) -> list[int]:
    """`candidates` 的合法位移全集 —— 暴力 oracle 的定義域。

    刻意跟 `candidates` 用同一套合法性定義(含 mid_lo),因為第 9 條測試要驗的
    是「clamp 在窗口內取到最佳」,不是「窗口該多寬」(那是第 6 條)。
    """
    tail = run.length - quantity
    if quantity < 1 or tail < 0:
        return []
    offsets: set[int] = set()
    if tail == 0 or tail >= policy.min_run:
        offsets |= {0, tail}
    offsets |= set(range(policy.mid_lo, tail - policy.mid_lo + 1))
    return sorted(offsets)


# 1 ─ 永不製造孤兒（隨機序列模擬）

def test_allocation_never_creates_an_orphan() -> None:
    rng = random.Random(20260803)
    for _ in range(200):
        caps = {bid: rng.randint(6, 30) for bid in range(1, rng.randint(2, 4) + 1)}
        geometry = {bid: BlockGeometry.calibrated(bid, cap) for bid, cap in caps.items()}
        occupied: dict[int, set[int]] = {bid: set() for bid in caps}

        for _ in range(60):
            quantity = rng.randint(1, 4)
            runs = [r for bid, cap in caps.items() for r in _free_runs(bid, cap, occupied[bid])]
            placed = allocate(runs, quantity, geometry)
            if placed is None:
                continue

            seats = range(placed.start, placed.start + placed.length)
            assert not (occupied[placed.block_id] & set(seats)), "配到已售出的座位"
            occupied[placed.block_id].update(seats)

            for bid, cap in caps.items():
                for run in _free_runs(bid, cap, occupied[bid]):
                    assert not 1 <= run.length < NORMAL_POLICY.min_run, (
                        f"block {bid} 出現長度 {run.length} 的孤兒段"
                    )


# 2 ─ 配出來的一定連續、且完整落在某個空段內

def test_placement_is_contiguous_and_inside_a_run() -> None:
    geometry = {1: BlockGeometry.calibrated(1, 30), 2: BlockGeometry.calibrated(2, 12)}
    runs = [Run(1, 4, 9), Run(1, 20, 6), Run(2, 0, 12)]
    for quantity in range(1, MAX_ORDER + 1):
        placed = allocate(runs, quantity, geometry)
        if placed is None:
            continue
        assert placed.length == quantity
        host = next(
            r
            for r in runs
            if r.block_id == placed.block_id
            and r.start <= placed.start
            and placed.start + placed.length <= r.start + r.length
        )
        assert host is not None


# 3 ─ 每個長度 L 的空段恰好配不出 L−1 張

@pytest.mark.parametrize("length", range(1, 10))
def test_the_only_infeasible_quantity_is_length_minus_one(length: int) -> None:
    geometry = {1: BlockGeometry.calibrated(1, length)}
    got = feasible_quantities([Run(1, 0, length)], geometry, MAX_ORDER)
    expected = [n for n in range(1, MAX_ORDER + 1) if n == length or n <= length - 2]
    assert got == expected
    if 2 <= length - 1 <= MAX_ORDER:
        assert length - 1 not in got


def test_feasible_quantities_is_not_max_contiguous() -> None:
    """只剩一段 5 連號時 4 張配不出來 —— max_contiguous 會誤導前端。"""
    geometry = {1: BlockGeometry.calibrated(1, 5)}
    assert feasible_quantities([Run(1, 0, 5)], geometry, MAX_ORDER) == [1, 2, 3, 5]


# 4 ─ cut_cost 的符號

def test_cut_cost_signs() -> None:
    p = NORMAL_POLICY
    perfect = cut_cost(p, run_length=4, left=0, right=0, taken=4)
    end_cut = cut_cost(p, run_length=20, left=0, right=19, taken=1)
    mid_cut = cut_cost(p, run_length=20, left=9, right=10, taken=1)
    eats_orphan = cut_cost(p, run_length=1, left=0, right=0, taken=1)
    makes_orphan = cut_cost(p, run_length=2, left=0, right=1, taken=1)

    assert perfect == pytest.approx(0.0)
    assert end_cut == pytest.approx(0.0)
    assert mid_cut == pytest.approx(0.0), "中切與端切成本必須相同,否則單人拿不到好位"
    assert eats_orphan < 0, "消孤兒必須是負成本,單人回填才會是公式的副產品"
    assert makes_orphan > 0
    assert eats_orphan == pytest.approx(-makes_orphan)


# 5 ─ 結果不依賴迭代順序（Lua 移植的前提）

def test_allocate_is_order_independent() -> None:
    geometry = {
        bid: BlockGeometry.calibrated(bid, 24, base=1.0 - 0.05 * bid) for bid in (1, 2, 3)
    }
    runs = [Run(1, 0, 11), Run(1, 13, 7), Run(2, 2, 20), Run(3, 0, 5), Run(3, 7, 9)]
    for quantity in range(1, MAX_ORDER + 1):
        reference = allocate(runs, quantity, geometry)
        rng = random.Random(quantity)
        for _ in range(20):
            shuffled = runs[:]
            rng.shuffle(shuffled)
            assert allocate(shuffled, quantity, geometry) == reference


# 6 ─ 中切只在窗口打得開時出現

def test_mid_cut_appears_only_when_window_is_open() -> None:
    geo = BlockGeometry.calibrated(1, 60)
    quantity, mid_lo = 2, NORMAL_POLICY.mid_lo
    for tail in range(0, 2 * mid_lo + 4):
        run = Run(1, 0, quantity + tail)
        got = list(candidates(run, quantity, geo, NORMAL_POLICY))
        if tail == 0:
            expected_ends = 1
        elif tail < NORMAL_POLICY.min_run:
            expected_ends = 0
        else:
            expected_ends = 2
        expected_mid = 1 if tail >= 2 * mid_lo else 0
        assert len(got) == expected_ends + expected_mid, f"tail={tail}"
        assert len({(c.block_id, c.start) for c in got}) == len(got), "候選重複"


# 7 ─ 品質校準與單峰性（clamp 的前提）

@pytest.mark.parametrize("capacity", [1, 2, 3, 5, 20, 41])
def test_calibrated_quality_is_unit_ranged_and_unimodal(capacity: int) -> None:
    geo = BlockGeometry.calibrated(1, capacity)
    qs = [geo.seat_quality(pos) for pos in range(capacity)]
    assert all(-1e-9 <= q <= 1 + 1e-9 for q in qs)
    assert max(qs) == pytest.approx(1.0)
    # 凹 ⇒ 單峰:一階差分不遞增。
    diffs = [b - a for a, b in zip(qs, qs[1:])]
    assert all(later <= earlier + 1e-9 for earlier, later in zip(diffs, diffs[1:]))


# 8 ─ 收尾期把洞填掉

@pytest.mark.parametrize("length", range(1, 10))
def test_endgame_policy_fills_the_hole(length: int) -> None:
    geometry = {1: BlockGeometry.calibrated(1, length)}
    got = feasible_quantities([Run(1, 0, length)], geometry, MAX_ORDER, ENDGAME_POLICY)
    assert got == list(range(1, min(length, MAX_ORDER) + 1))


# 9 ─ 暴力 oracle：clamp 真的取到窗口內最佳解

def test_clamp_matches_brute_force_over_all_legal_offsets() -> None:
    rng = random.Random(1234)
    checked = 0
    for _ in range(2000):
        capacity = rng.randint(1, 40)
        geo = BlockGeometry.calibrated(9, capacity, base=rng.uniform(0.4, 1.0))
        start = rng.randint(0, capacity - 1)
        run = Run(9, start, rng.randint(1, capacity - start))
        quantity = rng.randint(1, MAX_ORDER)

        best = allocate([run], quantity, {9: geo})
        offsets = _legal_offsets(run, quantity, NORMAL_POLICY)
        if not offsets:
            assert best is None
            continue

        brute = max(
            placement_at(run, offset, quantity, geo, NORMAL_POLICY).score
            for offset in offsets
        )
        assert best is not None
        assert best.score == pytest.approx(brute), (
            f"run={run} n={quantity}: 3 個候選漏掉了更好的解"
        )
        checked += 1
    assert checked > 500, "隨機參數幾乎都落在無候選的分支,這個測試沒測到東西"


# 10 ─ λ 的語意：清孤兒 vs 給單人好位

def _orphan_vs_good_seat(weight: float) -> Placement | None:
    """block 1 只剩 pos 5 一個孤兒(中等品質);block 2 整段空著(中央是好位)。"""
    geometry = {1: BlockGeometry.calibrated(1, 20), 2: BlockGeometry.calibrated(2, 20)}
    runs = [Run(1, 5, 1), Run(2, 0, 20)]
    return allocate(runs, 1, geometry, Policy(fragmentation_weight=weight))


def test_high_lambda_makes_the_single_clear_the_orphan() -> None:
    placed = _orphan_vs_good_seat(1.5)
    assert placed is not None
    assert (placed.block_id, placed.start) == (1, 5)


def test_low_lambda_gives_the_single_the_good_seat() -> None:
    placed = _orphan_vs_good_seat(0.5)
    assert placed is not None
    assert placed.block_id == 2


def test_single_picks_the_best_orphan_available() -> None:
    """同 tier 內用品質排序:孤兒不是隨便挑一個,是挑最靠中間的那個。"""
    geometry = {1: BlockGeometry.calibrated(1, 20)}
    runs = [Run(1, 0, 1), Run(1, 8, 1), Run(1, 18, 1)]
    placed = allocate(runs, 1, geometry)
    assert placed is not None
    assert placed.start == 8


# 11 ─ legal_anchors 與 allocate 是同一套邏輯

def test_allocate_is_the_top_legal_anchor() -> None:
    geometry = {1: BlockGeometry.calibrated(1, 30), 2: BlockGeometry.calibrated(2, 18)}
    runs = [Run(1, 0, 14), Run(1, 17, 13), Run(2, 3, 15)]
    for quantity in range(1, MAX_ORDER + 1):
        anchors = legal_anchors(runs, quantity, geometry)
        placed = allocate(runs, quantity, geometry)
        if not anchors:
            assert placed is None
            continue
        assert placed is not None
        assert placed.score == pytest.approx(anchors[0].score)


# 12 ─ occupy / release 的結構不變式（Lua 移植版的 oracle）

def test_occupy_splits_into_at_most_two_remainders() -> None:
    whole = [Run(1, 0, 20)]
    assert occupy(whole, Placement(1, 8, 4, 0.0)) == [Run(1, 0, 8), Run(1, 12, 8)]
    assert occupy(whole, Placement(1, 0, 4, 0.0)) == [Run(1, 4, 16)]
    assert occupy(whole, Placement(1, 16, 4, 0.0)) == [Run(1, 0, 16)]
    assert occupy(whole, Placement(1, 0, 20, 0.0)) == []


def test_occupy_rejects_a_placement_outside_every_run() -> None:
    with pytest.raises(ValueError, match="不完整落在"):
        occupy([Run(1, 0, 4), Run(1, 8, 4)], Placement(1, 3, 3, 0.0))


def test_release_absorbs_both_neighbours() -> None:
    # 空段 [0,3) 與 [5,9),歸還中間的 [3,5) → 三段併成一段。
    merged = release([Run(1, 0, 3), Run(1, 5, 4)], block_id=1, start=3, length=2)
    assert merged == [Run(1, 0, 9)]


def test_release_only_absorbs_touching_neighbours() -> None:
    # [0,3) 與 [6,9) 都不緊鄰 [4,5),所以維持三段。
    got = release([Run(1, 0, 3), Run(1, 6, 3)], block_id=1, start=4, length=1)
    assert got == [Run(1, 0, 3), Run(1, 4, 1), Run(1, 6, 3)]


def test_release_leaves_other_blocks_alone() -> None:
    got = release([Run(2, 0, 4)], block_id=1, start=0, length=2)
    assert got == [Run(1, 0, 2), Run(2, 0, 4)]


def test_release_rejects_a_double_release() -> None:
    with pytest.raises(ValueError, match="重複釋放"):
        release([Run(1, 0, 20)], block_id=1, start=5, length=2)


def test_occupy_then_release_is_the_identity() -> None:
    geometry = {1: BlockGeometry.calibrated(1, 24)}
    before = [Run(1, 0, 11), Run(1, 14, 10)]
    for quantity in range(1, MAX_ORDER + 1):
        placed = allocate(before, quantity, geometry)
        if placed is None:
            continue
        after = occupy(before, placed)
        restored = release(
            after,
            block_id=placed.block_id,
            start=placed.start,
            length=placed.length,
        )
        assert restored == sorted(before, key=lambda r: (r.block_id, r.start))


def test_runs_stay_disjoint_non_adjacent_and_conserve_seats() -> None:
    rng = random.Random(4242)
    caps = {bid: rng.randint(8, 26) for bid in (1, 2, 3)}
    geometry = {bid: BlockGeometry.calibrated(bid, cap) for bid, cap in caps.items()}
    runs = [Run(bid, 0, cap) for bid, cap in caps.items()]
    live: list[Placement] = []

    for _ in range(1500):
        if live and rng.random() < 0.35:
            victim = live.pop(rng.randrange(len(live)))
            runs = release(
                runs,
                block_id=victim.block_id,
                start=victim.start,
                length=victim.length,
            )
        else:
            placed = allocate(runs, rng.randint(1, 4), geometry)
            if placed is None:
                continue
            runs = occupy(runs, placed)
            live.append(placed)

        assert runs == sorted(runs, key=lambda r: (r.block_id, r.start)), "未正規化"
        for earlier, later in zip(runs, runs[1:]):
            if earlier.block_id != later.block_id:
                continue
            assert earlier.start + earlier.length < later.start, (
                f"{earlier} 與 {later} 重疊或緊鄰 —— 合併漏了"
            )
        assert sum(r.length for r in runs) + sum(p.length for p in live) == sum(
            caps.values()
        ), "座位總數沒守恆"


# 13 ─ 入口守衛：封住單峰性與參數語意

def test_geometry_rejects_negative_decay() -> None:
    """decay < 0 會讓品質變凹谷,clamp 靜默取到最差解 —— 在入口就擋掉。"""
    with pytest.raises(ValueError, match="decay"):
        BlockGeometry(block_id=1, capacity=20, base=1.0, decay=-0.1)


def test_geometry_rejects_an_empty_block() -> None:
    with pytest.raises(ValueError, match="capacity"):
        BlockGeometry(block_id=1, capacity=0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_run": 0},
        {"mid_cut_guard": 0},
        {"orphan_value": 1.5},
        {"orphan_value": -0.1},
        {"fragmentation_weight": -1.0},
    ],
)
def test_policy_rejects_nonsense(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        Policy(**kwargs)


# 14 ─ 裝箱效率的回歸下限（模擬，不是單元測試）

_SIM_SEEDS = (11, 22, 33, 44)
_SIM_BLOCKS = 12


def test_hard_constraint_costs_nothing_without_cancellations() -> None:
    """沒有棄單時,硬約束一張票都沒少賣、也沒留下任何孤兒。"""
    blocks = typical_venue(_SIM_BLOCKS)
    for seed in _SIM_SEEDS:
        result = simulate(blocks, TYPICAL_DEMAND, cancel_rate=0.0, seed=seed)
        assert result.orphans == 0
        assert result.sold_ratio >= 0.99, (seed, result)


def test_orphans_only_come_from_cancelled_singles() -> None:
    """需求裡沒有單人票 → 就算大量取消,孤兒數必須恆為 0。

    這是設計裡「孤兒的唯一來源是單人票取消」那句話的直接驗證:release 只會讓
    空段變長,所以新空段的長度下限就是訂單張數下限。
    """
    blocks = typical_venue(_SIM_BLOCKS)
    for seed in _SIM_SEEDS:
        result = simulate(blocks, NO_SINGLES_DEMAND, cancel_rate=0.3, seed=seed)
        assert result.orphans == 0, (seed, result)


def test_packing_stays_above_regression_floor() -> None:
    """30% 取消率下的售出率下限。擋的是「某次改動讓裝箱崩掉而沒人發現」。"""
    blocks = typical_venue(_SIM_BLOCKS)
    ratios = [
        simulate(blocks, TYPICAL_DEMAND, cancel_rate=0.3, seed=seed).sold_ratio
        for seed in _SIM_SEEDS
    ]
    assert min(ratios) >= 0.93, ratios
