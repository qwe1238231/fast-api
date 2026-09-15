"""量 event meta 快取的 stampede 防護在真的多 process 下擋掉多少 DB 讀。

單元測試只能證明單 process 內 N 個 miss → 1 次 DB 讀;這裡對著 docker compose 的
api(uvicorn --workers 4)打真的 HTTP burst,證明跨 process 那一層(Redis lock)也在。

**量的是 Postgres 這一側的真相**:pg_stat_statements 裡 `db.get(Event, id)` 那條語句的
calls 在 burst 前後的差值。不用 app 的 /metrics —— 4 個 worker 各自一份 registry,
scrape 只會拿到應答那一個 worker 的數字(這本身是個待修的觀測性缺口,見報告)。

三個模式:
  cold   把 key 弄冷,BURST 個 VU 同時各打一發          期望 DB 讀 = 1(不是 4,不是 300)
  warm   key 已暖,同樣一波                              期望 DB 讀 = 0
  churn  定速流量 6 秒,期間每 0.5 秒把 key 弄冷一次     期望 DB 讀 = 弄冷次數 + 1(每次冷 1 讀)

2026-09-15 實測(4 workers、300 併發):cold 1 / warm 0 / churn 11(10 次弄冷 + 起始冷)。
第一版量到三個 0 —— 兩個 bug 疊在一起:pg_stat 的比對樣式沒處理換行(量錯),以及
loader 跟請求搶同一個 pool 的死鎖(300 併發 → 149 個 504、真的 0 次讀)。
「量到 0」先懷疑量錯,再懷疑真的壞了,最後才相信防護完美。

前置:
  - api 容器跑著新映像且 LOADTEST_BYPASS_ADMISSION=True(否則每個請求先被等候室擋掉)
  - loadtest/tokens.json 沒過期(PYTHONPATH=. .venv/bin/python loadtest/seed.py)
  - 期間把 event 的 Redis 庫存設 0:每個請求都在快取讀**之後**被 409 擋下,不會往
    orders 表灌資料;結束後還原。

用法(repo root):
    PYTHONPATH=. .venv/bin/python loadtest/measure_cache_stampede.py            # 三個模式都跑
    MODES=cold EVENT_ID=1 BURST=300 PYTHONPATH=. .venv/bin/python loadtest/measure_cache_stampede.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.redis import create_redis_client
from app.core.config import get_settings
from app.db.session import engine

EVENT_ID = int(os.getenv("EVENT_ID", "1"))
BURST = int(os.getenv("BURST", "300"))
MODES = os.getenv("MODES", "cold,warm,churn").split(",")
CHURN_CYCLES = int(os.getenv("CHURN_CYCLES", "10"))
CHURN_INTERVAL_S = float(os.getenv("CHURN_INTERVAL_S", "0.5"))
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

K6_SCRIPT = Path(__file__).parent / "cache_stampede.js"
SUMMARY = Path("/tmp/k6_cache_stampede_summary.json")

# db.get(Event, id) 發出的語句。pg_stat_statements 可能有好幾列(schema 演進過,欄位
# 清單不同的版本各一列),所以用前綴 + WHERE 子句比對後 sum。
#
# **先把空白正規化再比對**:SQLAlchemy 在 FROM / WHERE 前面放的是換行,單一空格的
# ILIKE 永遠不中 —— 第一版就是這樣量出三個 0,看起來像防護完美,其實是量錯。
EVENT_PK_SELECT = text(r"""
    SELECT COALESCE(SUM(calls), 0)
    FROM pg_stat_statements
    WHERE regexp_replace(query, '\s+', ' ', 'g')
          ILIKE 'SELECT events.id%FROM events WHERE events.id = $1%'
""")

META_KEY = f"event:{EVENT_ID}:meta"
LOCK_KEY = f"lock:{META_KEY}"
STOCK_KEY = f"event:{EVENT_ID}:available"


async def pk_select_calls() -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(EVENT_PK_SELECT))


def run_k6(mode: str) -> dict:
    """跑 k6,回傳 summary 裡的 http_reqs 數、p95、非預期回應數。"""
    SUMMARY.unlink(missing_ok=True)
    cmd = ["k6", "run", "--quiet", "--summary-export", str(SUMMARY),
           "-e", f"MODE={mode}", "-e", f"EVENT_ID={EVENT_ID}", "-e", f"BURST={BURST}",
           "-e", f"BASE_URL={BASE_URL}", str(K6_SCRIPT)]
    proc = subprocess.run(cmd, cwd=K6_SCRIPT.parent, capture_output=True, text=True)
    if proc.returncode != 0 and not SUMMARY.exists():
        print(proc.stdout[-2000:], proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"k6 failed with {proc.returncode}")
    summary = json.loads(SUMMARY.read_text())
    metrics = summary["metrics"]

    def value(name: str, key: str) -> float:
        m = metrics.get(name, {})
        return float(m.get(key) if key in m else m.get("values", {}).get(key, 0.0))

    return {
        "requests": int(value("http_reqs", "count")),
        "p95_ms": round(value("http_req_duration", "p(95)"), 1),
        "failed": int(value("http_req_failed", "passes")),   # k6 的 rate 指標:passes = 為真的次數
        "thresholds_ok": proc.returncode == 0,
    }


async def warm_up() -> None:
    """一發 POST /orders 把 key 暖起來。跟 k6 走同一條路,只是不需要 300 個 VU。"""
    token = json.loads((K6_SCRIPT.parent / "tokens.json").read_text())[0]
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        r = await client.post(
            "/v1/orders/",
            json={"event_id": EVENT_ID, "quantity": 1},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid.uuid4()),
                "Admission-Token": "loadtest",
            },
        )
        assert r.status_code in (202, 409), f"warm-up got {r.status_code}: {r.text[:200]}"


async def churn_deleter(redis: Redis, cycles: int, interval: float) -> None:
    """k6 打的時候在旁邊反覆把 key 弄冷 —— 模擬 TTL 到期 / 後台 invalidate 的穩態尖峰。"""
    for _ in range(cycles):
        await asyncio.sleep(interval)
        await redis.delete(META_KEY)


async def measure(redis: Redis, mode: str) -> dict:
    if mode == "cold":
        await redis.delete(META_KEY, LOCK_KEY)
    elif mode == "warm":
        await redis.delete(LOCK_KEY)
        await warm_up()
        assert await redis.exists(META_KEY), "warm-up did not fill the cache"
    elif mode == "churn":
        await redis.delete(META_KEY, LOCK_KEY)

    before = await pk_select_calls()
    t0 = time.monotonic()
    if mode == "churn":
        deleter = asyncio.create_task(churn_deleter(redis, CHURN_CYCLES, CHURN_INTERVAL_S))
        k6 = await asyncio.to_thread(run_k6, "churn")
        await deleter
    else:
        k6 = await asyncio.to_thread(run_k6, "burst")
    elapsed = round(time.monotonic() - t0, 1)
    await asyncio.sleep(1.0)                                 # pg_stat 的統計不是同步更新的
    after = await pk_select_calls()

    return {"mode": mode, "db_reads": after - before, "elapsed_s": elapsed, **k6}


async def main() -> None:
    settings = get_settings()
    if not settings.DEBUG:
        raise SystemExit("refusing: this drives the bypass path, DEBUG must be on")
    redis = create_redis_client(settings.REDIS_URL)
    original_stock = await redis.get(STOCK_KEY)
    if original_stock is None:
        raise SystemExit(f"{STOCK_KEY} missing — is EVENT_ID={EVENT_ID} seeded?")
    try:
        await redis.set(STOCK_KEY, 0)                        # 全部 409:不落訂單,快取讀照樣發生
        results = [await measure(redis, m.strip()) for m in MODES]
    finally:
        await redis.set(STOCK_KEY, original_stock)
        await redis.delete(LOCK_KEY)
        await redis.aclose()

    # churn 一開始也先弄冷一次,所以是 cycles + 1
    expected = {"cold": "1", "warm": "0", "churn": str(CHURN_CYCLES + 1)}
    print(f"\nevent_id={EVENT_ID}  api={BASE_URL}  (uvicorn --workers 4)\n")
    print(f"{'mode':<7}{'requests':>9}{'db_reads':>10}{'expected':>10}{'p95_ms':>9}{'failed':>8}{'secs':>6}")
    for r in results:
        print(f"{r['mode']:<7}{r['requests']:>9}{r['db_reads']:>10}{expected.get(r['mode'], '?'):>10}"
              f"{r['p95_ms']:>9}{r['failed']:>8}{r['elapsed_s']:>6}")
    print("\ndb_reads = pg_stat_statements calls delta of `SELECT ... FROM events WHERE events.id = $1`")


if __name__ == "__main__":
    asyncio.run(main())
