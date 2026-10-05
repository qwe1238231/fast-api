#!/usr/bin/env python3
"""還原演練的驗證工具 —— 種資料、取指紋、破壞、比對。

用法(DATABASE_URL 從 terraform output 拿):

    export DATABASE_URL="$(cd infra && terraform output -raw database_url)"
    python infra/drill.py seed          # 種一個場次 + 20 筆訂單 + 哨兵
    python infra/drill.py fingerprint   # 記錄現況(寫到 infra/.drill_fingerprint.json)
    python infra/drill.py mutate        # 模擬「壞掉的 migration」:改哨兵 + 刪訂單
    python infra/drill.py verify        # 跟記錄的指紋比對,不符就 exit 1

**為什麼需要哨兵(sentinel),而不是只數訂單筆數:**

只檢查「訂單還在」的話,一個**根本沒還原、只是連回原本那台**的結果也會通過 ——
而那正是還原最可能默默失敗的方式(DATABASE_URL 還指著舊實例)。哨兵是一列會在
「快照之後、還原之前」被改掉的資料:驗證時看到舊值,才證明我們真的回到了過去,
而不是看到一個剛好也有資料的資料庫。

用原生 SQL 而不是走 app 的 ORM:這是**維運工具**,它要驗的是磁碟上的資料,不該
因為某天 model 改了欄位就跟著壞掉,也不該需要一份完整的 app 設定才能跑。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

FINGERPRINT_PATH = Path(__file__).parent / ".drill_fingerprint.json"

SENTINEL_BEFORE = "DRILL-BEFORE"
SENTINEL_AFTER = "DRILL-AFTER"
DRILL_VENUE = "DRILL-ARENA"          # 用它來認出演練資料,不會碰到別的場次
ORDER_COUNT = 20
DELETE_COUNT = 5


def _dsn() -> str:
    """terraform 輸出的是 SQLAlchemy 的 URL,asyncpg 不吃 `+asyncpg` 那一段。"""
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        sys.exit("DATABASE_URL 沒設。先跑:\n"
                 '  export DATABASE_URL="$(cd infra && terraform output -raw database_url)"')
    return raw.replace("postgresql+asyncpg://", "postgresql://")


async def _connect() -> asyncpg.Connection:
    # 逾時要短:連不上時我們想要一個明確的錯誤,而不是卡在那裡懷疑人生
    # (最常見的原因是 admin_cidr 跟目前的公開 IP 不符)。
    return await asyncpg.connect(_dsn(), timeout=15)


async def seed() -> None:
    conn = await _connect()
    try:
        now = datetime.now(timezone.utc)
        user_id = await conn.fetchval(
            """
            INSERT INTO users (username, hashed_password, is_active, is_admin)
            VALUES ($1, 'drill-not-a-real-hash', true, false)
            ON CONFLICT (username) DO UPDATE SET username = EXCLUDED.username
            RETURNING id
            """,
            "drill-buyer",
        )
        event_id = await conn.fetchval(
            """
            INSERT INTO events (name, venue, starts_at, ends_at, sale_starts_at,
                                sale_ends_at, total_seats, price_cents, status)
            VALUES ($1, $2, $3, $4, $5, $6, 1000, 1500, 'published')
            RETURNING id
            """,
            SENTINEL_BEFORE, DRILL_VENUE,
            now + timedelta(days=30), now + timedelta(days=30, hours=3),
            now - timedelta(days=1), now + timedelta(days=1),
        )
        await conn.executemany(
            """
            INSERT INTO orders (user_id, event_id, quantity, total_price_cents,
                                status, idempotency_key)
            VALUES ($1, $2, 1, 1500, 'pending', $3)
            """,
            [(user_id, event_id, uuid.uuid4()) for _ in range(ORDER_COUNT)],
        )
        print(f"種好了:event_id={event_id}(哨兵 name={SENTINEL_BEFORE})、"
              f"{ORDER_COUNT} 筆訂單、user_id={user_id}")
    finally:
        await conn.close()


async def _snapshot() -> dict:
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            """
            SELECT e.id            AS event_id,
                   e.name          AS sentinel,
                   count(o.id)     AS orders,
                   coalesce(max(o.id), 0) AS max_order_id
            FROM events e
            LEFT JOIN orders o ON o.event_id = e.id
            WHERE e.venue = $1
            GROUP BY e.id, e.name
            ORDER BY e.id
            LIMIT 1
            """,
            DRILL_VENUE,
        )
        if row is None:
            sys.exit(f"找不到演練資料(venue={DRILL_VENUE})—— 先跑 `drill.py seed`")
        return dict(row)
    finally:
        await conn.close()


async def fingerprint() -> None:
    state = await _snapshot()
    FINGERPRINT_PATH.write_text(json.dumps(state, indent=2))
    print(json.dumps(state, indent=2, ensure_ascii=False))
    print(f"→ 已記錄到 {FINGERPRINT_PATH.name}")


async def mutate(delete_only: bool = False) -> None:
    """模擬一支把資料寫壞的 migration。"""
    conn = await _connect()
    try:
        if not delete_only:
            await conn.execute(
                "UPDATE events SET name = $1 WHERE venue = $2", SENTINEL_AFTER, DRILL_VENUE
            )
        deleted = await conn.execute(
            """
            DELETE FROM orders WHERE id IN (
                SELECT o.id FROM orders o
                JOIN events e ON e.id = o.event_id
                WHERE e.venue = $1 ORDER BY o.id DESC LIMIT $2
            )
            """,
            DRILL_VENUE, DELETE_COUNT,
        )
        print(f"破壞完成:{'' if delete_only else f'哨兵 → {SENTINEL_AFTER}、'}{deleted}")
        print("現在的狀態(**這是「壞掉」的樣子,還原後不該看到它**):")
        print(json.dumps(await _snapshot(), indent=2, ensure_ascii=False))
    finally:
        await conn.close()


async def verify() -> None:
    if not FINGERPRINT_PATH.exists():
        sys.exit(f"沒有 {FINGERPRINT_PATH.name} —— 破壞之前要先 `drill.py fingerprint`")
    expected = json.loads(FINGERPRINT_PATH.read_text())
    actual = await _snapshot()

    diffs = {k: (expected[k], actual[k]) for k in expected if expected[k] != actual.get(k)}
    print("期望:", json.dumps(expected, ensure_ascii=False))
    print("實際:", json.dumps(actual, ensure_ascii=False))
    if diffs:
        for key, (want, got) in diffs.items():
            print(f"  ✗ {key}: 期望 {want!r},實際 {got!r}")
        if actual.get("sentinel") == SENTINEL_AFTER:
            print("\n哨兵還是 DRILL-AFTER —— 你連到的是**沒有被還原**的那個資料庫。"
                  "\n最可能的原因:DATABASE_URL 還指著舊實例(這正是這次演練要驗的事)。")
        sys.exit(1)
    print("\n✓ 還原正確:哨兵是還原前的值,訂單筆數與最大 id 都對得上。")


def main() -> None:
    commands = {
        "seed": seed,
        "fingerprint": fingerprint,
        "mutate": lambda: mutate("--delete-only" in sys.argv),
        "verify": verify,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        sys.exit(f"用法:drill.py [{' | '.join(commands)}]")
    asyncio.run(commands[sys.argv[1]]())


if __name__ == "__main__":
    main()
