"""Event meta 快取的 Prometheus 指標 —— 監控 stampede 防護是否真的在工作。

兩層防護(process 內 singleflight、跨 process Redis lock)各自的效果,只有一個數字
能證明:**每次 miss 平均換來幾次 DB 讀**。防護前 ≈ 1(每個 miss 各讀一次),
防護後應該 ≪ 1。這裡的兩個 counter 就是為了算出這個比值:

    DB 讀 = flights{loaded} + flights{fallback}
    miss  = requests{miss}
    同 process 內搭便車的人數 = requests{miss} − sum(flights)      ← 第 1 層的貢獻
    跨 process 等別人回填的次數 = flights{waited} + flights{already_filled}  ← 第 2 層的貢獻

判讀:

    flights{fallback} > 0(持續)   有 process 拿著 lock 死在重算裡,或重算慢過 lock TTL
                                    (3s)。前者查部署/OOM,後者查 DB —— 一次 PK 點查加
                                    一個 JOIN 要花 3 秒,快取不是問題所在。
    requests{miss} / requests 高    TTL(60s)對這個流量太短,或 invalidate 太頻繁。
    flights{waited} 的比例高        跨 process 合併確實在發生 —— 這是好事,但也代表
                                    第 2 層的輪詢延遲(最多 20ms 一輪)進了 p99。

刻意**沒有** event_id label:場次數不受控,而且判讀上需要的是全局比值,不是每場一條線。
"""
from prometheus_client import Counter

#: 每次 get_event_meta 的第一手結果。miss 不代表讀了 DB —— 那要看 flights。
EVENT_META_CACHE_REQUESTS = Counter(
    "event_meta_cache_requests_total",
    "event meta cache lookups by first-read outcome",
    ["outcome"],                      # hit | miss
)

#: 每個 singleflight flight(= 每個 process 的 leader)最後怎麼收場。
#: 四個值一一對應 event_cache._load_coordinated 的四條出口。
EVENT_META_CACHE_FLIGHTS = Counter(
    "event_meta_cache_flights_total",
    "singleflight leaders by how the miss was resolved",
    ["resolution"],                   # loaded | already_filled | waited | fallback
)
