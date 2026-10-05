"""模擬器的機械正確性。

這三個腳本驅動了四個架構決策(不移植 Lua、關掉中切、不做 compaction、收尾期用連拒
計數),但它們自己沒有任何測試 —— 而**腳本裡的 bug 會靜默產生錯的決策**。

這件事已經發生過兩次:第一版 compaction 量測把「賣完」算成「碎片拒絕」,得出
「compaction 沒用」;mid_cut_guard 掃了 2/3/4/6 卻沒掃「關掉」,得出「這個參數是
惰性的」。兩次都是**實驗設計**的錯,沒有任何測試抓得到。

但機械部分測得到:模擬器有沒有守住它宣稱的不變式、兩個變體有沒有真的看到同一份
需求序列、指標的分母對不對。那正是這個檔案的範圍。
"""
import random

import pytest

from app.scripts.simulate_compaction import (
    TYPICAL_DEMAND as COMPACTION_DEMAND,
    _compact,
    _script,
    simulate as simulate_compaction,
)
from app.scripts.simulate_seating import (
    TYPICAL_DEMAND,
    simulate as simulate_packing,
    typical_venue,
)
from app.services.seating import NORMAL_POLICY, Placement, Run


# ─ simulate_seating

@pytest.mark.parametrize("cancel_rate", [0.0, 0.3])
def test_packing_simulation_conserves_seats(cancel_rate: float) -> None:
    """售出 + 剩餘 == 總容量。少了這條,任何「售出率」的結論都可能是記帳錯誤。"""
    result = simulate_packing(
        typical_venue(10), TYPICAL_DEMAND, cancel_rate=cancel_rate, seed=7
    )
    assert 0 <= result.remaining <= result.total_seats
    assert result.sold_ratio == pytest.approx(
        (result.total_seats - result.remaining) / result.total_seats
    )


def test_packing_simulation_is_deterministic_per_seed() -> None:
    """跨 seed 平均才有意義的前提是**單一 seed 可重現**。"""
    blocks = typical_venue(10)
    first = simulate_packing(blocks, TYPICAL_DEMAND, cancel_rate=0.3, seed=99)
    second = simulate_packing(blocks, TYPICAL_DEMAND, cancel_rate=0.3, seed=99)
    assert first == second


def test_top_k_actually_changes_the_outcome() -> None:
    """top_k 參數若沒接上,「隨機化的裝箱代價」那張表會是一堆相同的數字 ——
    而「三個 K 值結果相同」正好也是我們用來論證「這是偵測器不是旋鈕」的證據。
    兩者長得一樣,所以必須確認參數真的有效。
    """
    blocks = typical_venue(10)
    deterministic = simulate_packing(blocks, TYPICAL_DEMAND, seed=5, top_k=1)
    randomized = simulate_packing(blocks, TYPICAL_DEMAND, seed=5, top_k=32)
    assert deterministic != randomized


# ─ simulate_compaction

def test_both_variants_see_the_same_demand() -> None:
    """兩個變體必須看到**完全相同**的訂單流,否則比較的是兩件不同的事。"""
    assert _script(random.Random(3), COMPACTION_DEMAND, abandon=0.4) == _script(
        random.Random(3), COMPACTION_DEMAND, abandon=0.4
    )


def test_compaction_returns_a_legal_layout() -> None:
    """重排後的每個 hold 都必須落在重排後的空段補集裡、且互不重疊。

    `_compact` 若回傳非法佈局,「compaction 讓 4 張被拒率減半」那個數字就是假的。
    """
    geometry = {b.block_id: b for b in typical_venue(4)}
    runs = [Run(bid, 0, geo.capacity) for bid, geo in geometry.items()]
    pending = [
        Placement(runs[0].block_id, 0, 2, 0.0),
        Placement(runs[0].block_id, 4, 3, 0.0),
        Placement(runs[1].block_id, 2, 4, 0.0),
    ]
    from app.services.seating import occupy

    for hold in pending:
        runs = occupy(runs, hold)

    result = _compact(runs, pending, geometry, NORMAL_POLICY, tight=True)
    assert result is not None
    new_runs, placed = result

    assert len(placed) == len(pending)
    assert sorted(p.length for p in placed) == sorted(p.length for p in pending)
    occupied: set[tuple[int, int]] = set()
    for hold in placed:
        seats = {(hold.block_id, pos) for pos in range(hold.start, hold.start + hold.length)}
        assert not (occupied & seats), "重排後的 hold 互相重疊"
        occupied |= seats
    free = {
        (run.block_id, pos)
        for run in new_runs
        for pos in range(run.start, run.start + run.length)
    }
    assert not (occupied & free), "重排後的 hold 落在被宣告為空的位置上"
    total = sum(geo.capacity for geo in geometry.values())
    assert len(occupied) + len(free) == total, "座位總數沒守恆"


def test_tight_fit_ignores_quality_and_packs_left() -> None:
    """壓緊的目標跟配位相反:配位要最好的位子,壓緊要最少的洞。

    這條擋的是「有人把 _tight_fit 換回 allocate」—— 那正是量測裡讓 4 張被拒率從
    12.0% 升到 14.4% 的做法。
    """
    from app.scripts.simulate_compaction import _tight_fit

    geometry = {b.block_id: b for b in typical_venue(3)}
    runs = sorted(
        (Run(bid, 0, geo.capacity) for bid, geo in geometry.items()),
        key=lambda r: (r.block_id, r.start),
    )
    spot = _tight_fit(runs, 2, geometry, NORMAL_POLICY)
    assert spot is not None
    assert (spot.block_id, spot.start) == (runs[0].block_id, 0), "必須是最左"


def test_fragmentation_metric_excludes_sold_out_rejections() -> None:
    """指標的分母只能包含「席數夠」的嘗試。

    第一版量測沒做這個過濾,於是 600 席吃 7800 席需求時被拒率全部 ~90% —— 那在量
    「賣完」而不是「碎片」,直接得出了「compaction 沒用」的錯誤結論。
    """
    result = simulate_compaction(
        COMPACTION_DEMAND, compaction="off", abandon=0.4, seed=1
    )
    assert result.sold / result.capacity > 0.9, "前提:這個設定會把場館賣到接近全滿"
    for quantity, tried in result.attempted.items():
        assert tried > 0
        assert result.rejected.get(quantity, 0) <= tried
        # 賣完之後的嘗試若被算進來,被拒率會逼近 1。
        assert result.reject_rate(quantity) < 0.5, quantity
