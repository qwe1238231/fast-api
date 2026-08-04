"""座位配位演算法 —— 純函式,零 I/O。

「哪裡可以坐」的判斷全部集中在這個模組:Redis / Postgres 那層只負責把 free
runs 讀出來、把選中的結果寫回去。因此這裡可以完整單元測試,也是之後 Lua
移植版的正確性 oracle(同樣輸入餵給兩邊,結果必須一致)。

核心設計:所有規則(不拆分、不製造孤兒、單人票回填、座位品質)統一成一個
潛勢函數 `Policy.phi` 與 `cut_cost`,程式碼裡沒有任何 `if quantity == 1`。
"""
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

#: 「孤兒」的長度。phi 只在這個長度上給折價,其餘按面值計。
_ORPHAN: Final = 1


@dataclass(frozen=True, slots=True)
class Policy:
    """配位策略。開賣期用 NORMAL_POLICY,收尾期換 ENDGAME_POLICY。

    單筆張數上限**不在**這裡 —— 那是業務規則(request schema / settings),
    不隨銷售階段變動。這裡只放「同樣的訂單,不同階段要怎麼配」。
    """

    min_run: int = 2
    """端點切之後的殘餘不得落在 [1, min_run)。設 1 等於允許製造孤兒。"""

    allow_mid_cut: bool = False
    """允不允許從空段中間切(而不是只從兩端)。

    **預設關閉,因為實測它用庫存換品質、比例約 1:1。** 中切會把一段偶數長度的
    空位切成兩段奇數,而奇數空段在均勻需求下必然擱淺(5 → 賣掉 2 → 剩 3 → 再賣 2
    會留下孤兒 → 禁止 → 3 席死掉)。量測(app/scripts/simulate_seating):

        需求            中切ON        中切OFF
        混合 1/2/3/4    100.0%       100.0%      ← 完全沒有貢獻
        只有 2 人票       88.0%        94.0%
        只有 3 人票       84.0%        90.0%
        2 與 4           85.0%        94.0%

    混合需求下連座位品質都一模一樣(0.3473 vs 0.3473)。唯一值得打開的情況是
    **確定賣不完的場次** —— 那時擱淺的席位本來也賣不掉,而品質是唯一的槓桿。
    """

    mid_cut_guard: int = 4
    """中切時兩側剩餘的下限(僅在 allow_mid_cut 時生效)。設成「單筆張數上限」
    代表切完兩側都還能服務任何一張合法訂單,可服務能力零損失。"""

    orphan_value: float = 0.3
    """φ(1):一個孤兒座之後真的被單人票買走的機率。單人票佔比越低就設越低。"""

    fragmentation_weight: float = 1.5
    """λ:碎片成本換算成座位品質的匯率。物理意義是「願意讓單人票的座位品質
    降多少,換一個孤兒被清掉」。孤兒勝出的臨界值是 (q_好 − q_孤) / (1 − φ(1))。"""

    def __post_init__(self) -> None:
        if self.min_run < 1:
            raise ValueError("min_run 至少為 1(1 = 允許製造孤兒)")
        if self.mid_cut_guard < 1:
            raise ValueError("mid_cut_guard 至少為 1")
        if not 0.0 <= self.orphan_value <= 1.0:
            raise ValueError("orphan_value 是機率,必須落在 [0, 1]")
        if self.fragmentation_weight < 0:
            raise ValueError("λ 為負會讓演算法反過來主動製造碎片")

    @property
    def mid_lo(self) -> int:
        """中切兩側的實際下限。

        至少 2:若允許某側只剩 1,φ(1) 的折價會讓 cut_cost 在中切窗口內不再
        是常數,`candidates` 用 clamp 取代枚舉就不保證取到最佳解。收尾期的
        放寬因此只作用在端點切(邊緣可以留孤兒),中切永遠不在中間挖孤兒。
        """
        return max(2, self.mid_cut_guard)

    def phi(self, length: int) -> float:
        """一段長度 length 的連續空位,期望最終能賣掉幾個座位。"""
        if length == 0:
            return 0.0
        if length == _ORPHAN:
            return self.orphan_value
        return float(length)


NORMAL_POLICY: Final = Policy()
#: 收尾期:只放寬「端點切可以留下孤兒」。不動 mid_cut_guard —— allow_mid_cut
#: 預設關閉,所以那個欄位在這裡完全不生效,寫上去只會讓人以為中切也放寬了。
ENDGAME_POLICY: Final = Policy(min_run=1)


@dataclass(frozen=True, slots=True)
class BlockGeometry:
    """一個 block(走道之間不可跨越的一段座位)的座位品質幾何。

    品質對 pos 必須**單峰** —— 那是 `candidates` 用 clamp 取代枚舉的前提。
    不規則因素(樓層、柱子遮蔽、視角)一律吸收進 `base`;需要更細的粒度就切
    更多 block,不要在 block 內部破壞單峰。
    """

    block_id: int
    capacity: int
    base: float = 1.0
    """品質在幾何中心的外插值。偶數 capacity 的中心落在兩座之間,沒有座位真的
    取到這個值 —— 所以請用 `calibrated()` 建構,不要手填。"""

    decay: float = 0.0
    """每離中心一個座位扣多少品質。**必須 >= 0**,見 __post_init__。"""

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("capacity 至少為 1")
        if self.decay < 0:
            # base − decay·|pos − center| 在 decay >= 0 時必定是凹函數,也就必定
            # 單峰;decay < 0 會翻成凹谷(最好的位子跑到兩端),此時 candidates 的
            # clamp 會**靜默地**取到窗口內最差解 —— 沒有例外、沒有錯誤,只是位子
            # 配得很怪,而那種 bug 沒有人會回報。單峰性在入口一次擋掉。
            raise ValueError("decay 必須 >= 0,否則品質不再單峰,clamp 取到的不是最佳解")

    @classmethod
    def calibrated(
        cls,
        block_id: int,
        capacity: int,
        *,
        base: float = 1.0,
        edge: float = 0.0,
    ) -> "BlockGeometry":
        """建一個「最中間的座位得 base、最邊緣的座位得 edge」的幾何。

        校準是必要的而不是方便:score 拿 quality 跟 cut_cost 相減,而 cut_cost
        的單位是「座位數」,所以每座品質必須落在 [0, 1],否則 λ 就失去意義。

        關鍵是要對「可達的距離範圍」正規化,不是對 center 正規化。偶數 capacity
        的最中間座位距中心 0.5 而非 0;若用 center 當分母,capacity=2 的 block
        會兩座都拿到 edge(全 0),於是一個位置絕佳的雙人小 block 在跨 block
        比較時被當成毫無價值。
        """
        center = (capacity - 1) / 2.0
        nearest = center % 1.0                 # 奇數 0.0,偶數 0.5
        span = center - nearest                # 最中間到最邊緣的距離差
        decay = 0.0 if span == 0 else (base - edge) / span
        return cls(
            block_id=block_id,
            capacity=capacity,
            base=base + decay * nearest,       # 外插回中心,使 d=nearest 時恰為 base
            decay=decay,
        )

    @property
    def center(self) -> float:
        return (self.capacity - 1) / 2.0

    def seat_quality(self, pos: int) -> float:
        return self.base - self.decay * abs(pos - self.center)

    def window_quality(self, start: int, length: int) -> float:
        """[start, start + length) 的品質總和。

        length 不超過單筆張數上限(4~6),直接加總比閉式解好維護太多。
        """
        return sum(self.seat_quality(pos) for pos in range(start, start + length))

    def ideal_start(self, length: int) -> int:
        """不考慮佔用時,length 個座位對稱跨在中心上的起始 pos。"""
        return round(self.center - (length - 1) / 2.0)


@dataclass(frozen=True, slots=True)
class Run:
    """一段極大連續空位 [start, start + length)。不跨 block。"""

    block_id: int
    start: int
    length: int


@dataclass(frozen=True, slots=True)
class Placement:
    """一個候選配位:從 start 起連續 length 個座位。"""

    block_id: int
    start: int
    length: int
    score: float


def cut_cost(
    policy: Policy,
    *,
    run_length: int,
    left: int,
    right: int,
    taken: int,
) -> float:
    """這一刀相對於「只損失賣出的 taken 個座位」的超額潛勢損失。

    == 0  乾淨的一刀(完美貼合、端點切、兩側都夠寬的中切)
    >  0  製造了難賣的殘料
    <  0  順手把本來賣不掉的孤兒變現了

    消孤兒的成本是負的,所以「單人票優先回填孤兒」不需要特例分支 —— 它是
    這個公式的副產品。
    """
    return (
        policy.phi(run_length)
        - policy.phi(left)
        - policy.phi(right)
        - taken
    )


def placement_at(
    run: Run,
    offset: int,
    quantity: int,
    geo: BlockGeometry,
    policy: Policy,
) -> Placement:
    """在 run 內位移 offset 處配 quantity 個座位並評分。**不檢查合法性**。

    合法性由 `candidates` 決定(過濾),取捨由這裡的 score 決定(排序)。兩者
    分層,不要混在一起。
    """
    start = run.start + offset
    return Placement(
        block_id=run.block_id,
        start=start,
        length=quantity,
        score=geo.window_quality(start, quantity)
        - policy.fragmentation_weight
        * cut_cost(
            policy,
            run_length=run.length,
            left=offset,
            right=run.length - quantity - offset,
            taken=quantity,
        ),
    )


def candidates(
    run: Run,
    quantity: int,
    geo: BlockGeometry,
    policy: Policy,
) -> Iterator[Placement]:
    """一個 run 產生 2 個候選(靠左端、靠右端),每個 O(1)。

    `allow_mid_cut` 打開時才有第三個。`allow_mid_cut` 打開時多一個中切點:
    中切窗口是連續區間、且窗口內 cut_cost 恆為常數,所以「品質對位置單峰」
    保證 clamp 取到的就是窗口內最佳 —— 不必枚舉 tail + 1 個位置。
    (單峰性正是 BlockGeometry 擋掉 decay < 0 的原因。)

    tail == 1 時一個候選都不產生。這就是「長度 L 的空段配不出 L − 1 張」的
    來源:每個空段的可配集合恰好是 {L} ∪ [1, L − 2]。
    """
    tail = run.length - quantity
    if quantity < 1 or tail < 0:
        return

    if tail == 0 or tail >= policy.min_run:                 # 端點切
        yield placement_at(run, 0, quantity, geo, policy)
        if tail > 0:                                        # tail == 0 時兩端同一刀
            yield placement_at(run, tail, quantity, geo, policy)

    if not policy.allow_mid_cut:
        return
    lo, hi = policy.mid_lo, tail - policy.mid_lo            # 中切窗口
    if lo <= hi:
        ideal = geo.ideal_start(quantity) - run.start
        yield placement_at(run, min(max(ideal, lo), hi), quantity, geo, policy)


def allocate(
    runs: Sequence[Run],
    quantity: int,
    geometry: Mapping[int, BlockGeometry],
    policy: Policy = NORMAL_POLICY,
) -> Placement | None:
    """得分最高的合法配位。

    回傳 None 代表「這個張數當下配不出來」,**不等於賣完** —— 呼叫端必須把
    它對應成 NO_FIT 而不是 SOLD_OUT,否則使用者會被告知售完卻看得到空位。
    """
    best: Placement | None = None
    best_key: tuple[float, int, int] | None = None
    for run in runs:
        geo = geometry[run.block_id]
        for cand in candidates(run, quantity, geo, policy):
            # 全序的 tiebreak,所以結果不依賴 runs 的迭代順序 —— Lua 那邊
            # HGETALL 的 field 順序是未定義的,靠這個 key 才對得起來。
            key = (cand.score, -cand.block_id, -cand.start)
            if best_key is None or key > best_key:
                best, best_key = cand, key
    return best


def legal_anchors(
    runs: Sequence[Run],
    quantity: int,
    geometry: Mapping[int, BlockGeometry],
    policy: Policy = NORMAL_POLICY,
) -> list[Placement]:
    """手動選位的可選集合,依建議度排序。

    自動配位就是這個列表取第一名(`allocate` == `legal_anchors()[0]`)。同一
    份邏輯支撐兩種產品:把約束編碼進「可選集合」,而不是在提交時才拒絕。
    """
    return sorted(
        (
            cand
            for run in runs
            for cand in candidates(run, quantity, geometry[run.block_id], policy)
        ),
        key=lambda p: (-p.score, p.block_id, p.start),
    )


def occupy(runs: Sequence[Run], placement: Placement) -> list[Run]:
    """套用一個配位,回傳新的空段集合(正規化排序)。

    被選中的空段拆成 0~2 段殘餘 —— **中切會產生兩段**,這是最容易在移植時漏掉
    的分支。對應 Lua 那邊「刪掉一個 run 的兩個 hash 條目,寫回 0~2 個」。
    """
    end = placement.start + placement.length
    out: list[Run] = []
    host: Run | None = None
    for run in runs:
        contains = (
            run.block_id == placement.block_id
            and run.start <= placement.start
            and end <= run.start + run.length
        )
        if not contains:
            out.append(run)
            continue
        host = run
        if placement.start > run.start:
            out.append(Run(run.block_id, run.start, placement.start - run.start))
        if end < run.start + run.length:
            out.append(Run(run.block_id, end, run.start + run.length - end))
    if host is None:
        raise ValueError(f"{placement} 不完整落在任何空段內")
    return sorted(out, key=lambda r: (r.block_id, r.start))


def release(
    runs: Sequence[Run], *, block_id: int, start: int, length: int
) -> list[Run]:
    """歸還 [start, start + length) 的座位,並與左右**緊鄰**的空段合併。

    純函式版是 O(R) 掃描;Redis 版靠 end→start 的反向索引(boundary tags)做到
    O(1)。兩者行為必須逐位一致 —— 這個函式就是那邊的 oracle。

    與既有空段重疊會 raise:那代表同一批座位被釋放兩次,結構一旦壞掉,下一次
    合併會把兩個 run 併成宣稱同一批座位的一個,直接導致重複售出。Redis 那邊靠
    `released:{marker}` 的 SETNX 擋掉同一件事 —— 它防的不只是多算幾個座位。
    """
    if length < 1:
        raise ValueError("length 至少為 1")
    lo, hi = start, start + length
    out: list[Run] = []
    for run in runs:
        if run.block_id != block_id:
            out.append(run)
            continue
        run_lo, run_hi = run.start, run.start + run.length
        if run_hi < lo or run_lo > hi:          # 有空隙 → 不相干
            out.append(run)
            continue
        if run_lo < hi and lo < run_hi:         # 真的重疊 → 重複釋放
            raise ValueError(f"[{lo}, {hi}) 與既有空段 {run} 重疊 —— 重複釋放")
        lo, hi = min(lo, run_lo), max(hi, run_hi)   # 緊鄰 → 吸收
    out.append(Run(block_id, lo, hi - lo))
    return sorted(out, key=lambda r: (r.block_id, r.start))


def feasible_quantities(
    runs: Sequence[Run],
    geometry: Mapping[int, BlockGeometry],
    max_order: int,
    policy: Policy = NORMAL_POLICY,
) -> list[int]:
    """當下配得出來的張數,供前端直接 disable 掉配不出來的選項。

    刻意由 `candidates` 推導,而不是另寫一份 {L} ∪ [1, L − 2] 的公式:兩份
    約束邏輯遲早會漂移,而漂移的後果是使用者送出註定失敗的請求。

    注意這不能簡化成「最大連號長度」—— 只剩一段 5 連號時 4 張是配不出來的
    (會留下孤兒),回 max_contiguous=5 然後拒絕 4 張正是客服災難的來源。
    """
    return [
        n
        for n in range(1, max_order + 1)
        if any(
            next(candidates(run, n, geometry[run.block_id], policy), None) is not None
            for run in runs
        )
    ]
