"""座位配位的 Prometheus 指標 —— 監控「不移植 Lua」那個決定的支點。

配位走的是「讀 runs → 在應用層算 → 短 Lua 做 CAS」,而不是把整套演算法搬進 Lua。
那個決定完全掛在一個數字上:

    N = λ × T        N = 同時看到同一份快照的請求數
                     λ = 該 zone 的下單速率
                     T = 讀→算→CAS 的時間窗

模擬(app/scripts/simulate_cas_contention.py)在 T ≈ 1.1ms 的假設下算出:預設放行
速率 500/秒 → N ≈ 0.55 → 幾乎不會互撞。但 **T 是估的不是量的** —— 它假設兩趟
Redis round-trip 約 1ms。N 隨 T **線性**成長:T=10ms 時 λ=500 就給出 N=5,
K=1 只剩 20% 成功率。

所以這裡量的就是 T,以及它的後果(CAS 重試)。

判讀門檻:

    seat_cas_window_seconds p99 > 0.005    T 已經是假設的 5 倍。TOP_K=16 還撐得住,
                                           但要開始查為什麼慢(Redis 飽和?跨 AZ?)
    seat_cas_window_seconds p99 > 0.010    N 進入模擬中會出現重試放大的區間
    retry 佔 attempts > 5%(持續)          衝突已經真實發生,不再是理論
    seat_contention_total > 0              重試已經被耗盡 —— 有人拿到 503

任何一條越線,就是「把配位搬進 Lua」的訊號 —— 屆時 app/services/seating.py 的
純函式就是現成的 oracle,差異測試的骨架也已經在 test_seat_runs.py 裡。
"""
from prometheus_client import Counter, Histogram

#: 讀→算→CAS 的時間窗。桶刻意集中在次毫秒到十毫秒之間 —— Prometheus 的預設桶
#: 從 5ms 起跳,對這裡完全無用(整個有意義的範圍都會落進第一個桶)。
SEAT_CAS_WINDOW = Histogram(
    "seat_cas_window_seconds",
    "One read-runs → compute → CAS attempt, the T in N = lambda * T",
    buckets=(0.0005, 0.001, 0.002, 0.003, 0.005, 0.01, 0.025, 0.05, 0.1),
)

#: 每次 CAS 嘗試的結果。retry / attempts 就是衝突率,直接對應模擬裡的「放大倍數」。
SEAT_CAS_ATTEMPTS = Counter(
    "seat_cas_attempts_total",
    "Seat CAS attempts by outcome",
    ["outcome"],                      # ok | retry | dup | bounds
)

#: 一筆成交用掉幾次 CAS。模擬裡 λ=2000 且不隨機化時是 3.08,K=16 時是 1.03。
SEAT_CAS_ATTEMPTS_PER_RESERVATION = Histogram(
    "seat_cas_attempts_per_reservation",
    "CAS attempts spent on one successful reservation",
    buckets=(1, 2, 3, 4, 5),
)

#: 重試被耗盡。**非零就代表有人拿到 503** —— 這不是可容忍的常態值。
SEAT_CONTENTION = Counter(
    "seat_contention_total",
    "Reservations that exhausted MAX_CAS_ATTEMPTS",
)
