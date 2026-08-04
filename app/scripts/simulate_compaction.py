"""Compaction 值不值得做?

混合模式(下單即時預留區段、確認後才定位)的整個回報都在這裡:pending 的 hold
沒有對外公開座號,所以**可以偷偷滑動**。過期的訂單會在已售出的座位之間留下洞,
把還沒確認的鄰居往旁邊挪就能把洞併回大段 —— 讓後期的大團體還買得到連號。

它**不會多賣一張票**(總席數不變),所以指標是「大團體訂單的被拒率」。

先量**上界**(全域重排、不加保護、每次釋放都做),再逐項退回真實實作能做到的樣子。
結論寫在下面 —— 這個腳本存在的理由是那個結論**推翻了原本的設計**。

量測結果(棄單率 40%,「4 張被拒」= 席數夠卻湊不出連號的比率):

    變體                        4 張被拒   搬動次數   品質變化
    不做 compaction              12.0%        0    +0.000
    壓緊:全域,每次釋放              5.8%      834    -0.032
    壓緊:單一 block,每次            6.2%      214    -0.085   ← 買得起的版本
    壓緊:單一 block,每 10 次       12.6%       22    -0.178   ← 比不做還差
    壓緊 + Pareto 保護            10.8%        3    +0.074   ← 保護把機制關死
    品質目標(重用 allocate)       14.4%      968    +0.024   ← 比不做還差

三個推翻原設計的發現:

1. **不能重用 `allocate()`。** 原本的設計說「重用 allocate 是整個設計最省的地方」,
   但 allocate 是品質貪婪的 —— 拿它重排等於把 hold 重新撒到全場品質最高的空位,
   contiguity 反而被打散,4 張被拒率從 12% 升到 14.4%。壓緊的目標函數跟配位相反:
   配位要「最好的位子」,壓緊要「最少的洞」。

2. **Pareto 保護(沒人變差才套用)會把機制關死。** 834 次搬動剩 3 次。而且它保護的
   是一個不存在的傷害:確認前不吐座號,所以買家從來不知道自己「本來會坐哪」。

3. **批次會抹掉效益。** 洞如果不在下一筆大訂單到來前補起來,補了也沒用。掛在每分鐘
   的掃描上(每 10 次才壓一次)的版本比不做還差。壓緊必須跟「製造那個洞」的動作
   在同一個 pass 裡完成。

而且效益高度依賴棄單率:棄單 20% 時單一 block 版本是 5.9% vs 不做的 6.0%,量不出
差別。總營收影響是零(兩種做法都賣光 100%),差別純粹在「看得到位子卻湊不出連號」
的拒絕次數 —— 大約佔全部訂單的 3%。

    python -m app.scripts.simulate_compaction
"""
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.scripts.simulate_seating import TYPICAL_DEMAND, typical_venue
from app.services.seating import (
    NORMAL_POLICY,
    BlockGeometry,
    Placement,
    Policy,
    Run,
    allocate,
    candidates,
    occupy,
    release,
)

STEPS = 6000
SEEDS = tuple(range(31337, 31337 + 12))


@dataclass(frozen=True, slots=True)
class Outcome:
    sold: int
    capacity: int
    rejected: Mapping[int, int]
    attempted: Mapping[int, int]
    """只計「當下剩餘席數 >= 張數」的嘗試 —— 也就是**位子夠但湊不出連號**的情況。

    不做這個過濾的話,整場的被拒率會被「已經賣完」淹沒(600 席吃 7800 席的需求,
    九成的拒絕只是因為沒票了),而那跟 compaction 想解決的問題無關。
    """
    moves: int
    """compaction 實際搬動了幾個 hold。0 = 這個機制從沒派上用場。"""

    proposals: int
    rejected_proposals: int
    """被 Pareto 保護擋掉的提案數。接近 proposals = 保護把機制關死了。"""

    quality_delta: float
    """被搬動的 hold 平均品質變化(每座)。負值 = 買家被降級了。"""

    def reject_rate(self, quantity: int) -> float:
        tried = self.attempted.get(quantity, 0)
        return self.rejected.get(quantity, 0) / tried if tried else 0.0


def _script(rng: random.Random, demand: Mapping[int, float], *, abandon: float):
    """先把「需求序列」定死,兩個變體才看到完全相同的訂單流。"""
    quantities, weights = list(demand), list(demand.values())
    out = []
    for _ in range(STEPS):
        if rng.random() < 0.5:
            out.append(("resolve", rng.random(), rng.random() < abandon))
        else:
            out.append(("new", rng.choices(quantities, weights=weights, k=1)[0], False))
    return out


def _tight_fit(
    runs: Sequence[Run],
    quantity: int,
    geometry: Mapping[int, BlockGeometry],
    policy: Policy,
) -> Placement | None:
    """最左優先的 first-fit —— **不看座位品質**。

    compaction 的目標跟配位相反:配位要「最好的位子」,壓緊要「最少的洞」。
    用同一個 allocate() 去重排,等於把 hold 重新撒到全場品質最高的空位上,
    contiguity 反而被打散(實測 4 張的被拒率因此翻倍)。
    """
    for run in sorted(runs, key=lambda r: (r.block_id, r.start)):
        for spot in candidates(run, quantity, geometry[run.block_id], policy):
            return spot          # candidates 先產左端 → 這就是最左的合法位置
    return None


def _compact(
    runs: Sequence[Run],
    pending: Sequence[Placement],
    geometry: Mapping[int, BlockGeometry],
    policy: Policy,
    *,
    tight: bool,
) -> tuple[list[Run], list[Placement]] | None:
    """把所有 pending 區間視為空,再用 First-Fit-Decreasing 重排。

    大的先放:小的先放會把大段啃出洞,之後大的無處可去。任何一筆放不回去就整包
    放棄 —— 「全部留在原位」永遠是合法佈局,所以放棄永遠安全。
    """
    freed = list(runs)
    for hold in pending:
        freed = release(
            freed, block_id=hold.block_id, start=hold.start, length=hold.length
        )

    placed: list[Placement] = []
    for hold in sorted(pending, key=lambda h: -h.length):
        spot = (
            _tight_fit(freed, hold.length, geometry, policy)
            if tight
            else allocate(freed, hold.length, geometry, policy)
        )
        if spot is None:
            return None
        freed = occupy(freed, spot)
        placed.append(spot)
    return freed, placed


def simulate(
    demand: Mapping[int, float],
    *,
    compaction: str,
    abandon: float,
    seed: int,
    policy: Policy = NORMAL_POLICY,
    pareto: bool = False,
    every: int = 1,
    scope: str = "global",
) -> Outcome:
    blocks = typical_venue()
    geometry = {b.block_id: b for b in blocks}
    runs = [Run(b.block_id, 0, b.capacity) for b in blocks]
    capacity = sum(b.capacity for b in blocks)

    pending: list[Placement] = []
    rejected: Counter[int] = Counter()
    attempted: Counter[int] = Counter()
    moves = proposals = rejected_proposals = releases = 0
    quality_deltas: list[float] = []

    def window(hold: Placement) -> float:
        return geometry[hold.block_id].window_quality(hold.start, hold.length)

    for action in _script(random.Random(seed), demand, abandon=abandon):
        if action[0] == "resolve":
            if not pending:
                continue
            victim = pending.pop(int(action[1] * len(pending)))
            if not action[2]:
                continue                      # 確認 → 凍結,不再可動
            runs = release(
                runs, block_id=victim.block_id,
                start=victim.start, length=victim.length,
            )
            releases += 1
            # 真實實作掛在每分鐘的過期掃描上,不是每次釋放都跑 —— every 模擬那個批次。
            if compaction != "off" and pending and releases % every == 0:
                proposals += 1
                # 只重排「洞所在的那個 block」:洞是局部的,而全域重排要動的
                # hold 數量跟整個 zone 的 pending 量成正比,買不起。
                movable = (
                    [h for h in pending if h.block_id == victim.block_id]
                    if scope == "block"
                    else list(pending)
                )
                frozen_rest = [h for h in pending if h not in movable]
                if not movable:
                    continue
                result = _compact(
                    runs, movable, geometry, policy, tight=compaction == "tight"
                )
                if result is None:
                    rejected_proposals += 1
                else:
                    new_runs, replaced = result
                    before = sorted(movable, key=lambda h: -h.length)
                    # Pareto 保護:任何一個 hold 的座位品質變差就整包放棄。
                    # 「全部留在原位」永遠是合法佈局,所以放棄永遠安全。
                    if pareto and any(
                        window(b) < window(a) - 1e-9 for a, b in zip(before, replaced)
                    ):
                        rejected_proposals += 1
                    else:
                        for a, b in zip(before, replaced):
                            if (a.block_id, a.start) != (b.block_id, b.start):
                                moves += 1
                                quality_deltas.append(
                                    (window(b) - window(a)) / a.length
                                )
                        runs, pending = new_runs, replaced + frozen_rest
            continue

        quantity = action[1]
        remaining = sum(r.length for r in runs)
        countable = remaining >= quantity      # 席數夠 → 這一筆的成敗才反映形狀
        if countable:
            attempted[quantity] += 1
        spot = allocate(runs, quantity, geometry, policy)
        if spot is None:
            if countable:
                rejected[quantity] += 1
            continue
        runs = occupy(runs, spot)
        pending.append(spot)

    return Outcome(
        sold=capacity - sum(r.length for r in runs),
        capacity=capacity,
        rejected=dict(rejected),
        attempted=dict(attempted),
        moves=moves,
        proposals=proposals,
        rejected_proposals=rejected_proposals,
        quality_delta=(
            sum(quality_deltas) / len(quality_deltas) if quality_deltas else 0.0
        ),
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def main() -> None:
    print(
        f"{len(SEEDS)} seeds × {STEPS} steps × 600 座\n"
        f"數字 = 席數夠卻配不出來的比率(排除掉單純賣完的拒絕)\n"
    )
    header = (
        f"{'情境':<26}{'售出率':>9}"
        + "".join(f"{f'{n} 張':>9}" for n in (1, 2, 3, 4))
        + f"{'搬動次數':>10}{'品質變化':>11}"
    )
    for abandon in (0.2, 0.4):
        print(f"=== 棄單率 {abandon:.0%} ===")
        print(header)
        for label, mode, pareto, every, scope in (
            ("不做 compaction", "off", False, 1, "global"),
            ("壓緊:全域,每次釋放", "tight", False, 1, "global"),
            ("壓緊:單一 block,每次", "tight", False, 1, "block"),
            ("壓緊:單一 block,每 10 次", "tight", False, 10, "block"),
            ("壓緊 + Pareto 保護", "tight", True, 1, "global"),
        ):
            runs = [
                simulate(
                    TYPICAL_DEMAND, compaction=mode, abandon=abandon,
                    seed=s, pareto=pareto, every=every, scope=scope,
                )
                for s in SEEDS
            ]
            cells = "".join(
                f"{_mean([r.reject_rate(n) for r in runs]):>9.1%}" for n in (1, 2, 3, 4)
            )
            print(
                f"{label:<26}{_mean([r.sold / r.capacity for r in runs]):>9.1%}"
                f"{cells}{_mean([r.moves for r in runs]):>10.0f}"
                f"{_mean([r.quality_delta for r in runs]):>+11.3f}"
            )
        print()


if __name__ == "__main__":
    main()
