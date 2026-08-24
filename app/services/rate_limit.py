"""限流:Redis 固定窗。**這是專案裡唯一的一套。**

以前這裡跟 slowapi 並存,兩套各自正確,但「限流要怎麼做」這個決定被做了兩次,而它們
在三件事上不一致:

  - **key 怎麼算** —— slowapi 用一個全域 `key_func`,這裡用呼叫端給的字串。於是
    「代理後面要取哪一跳」這個問題只在其中一邊被想過(而且兩邊一開始都想錯了)。
  - **計數存在哪** —— slowapi 預設 `memory://`,每個 process 一份;這裡一直是 Redis。
    踩過一次:`--workers 4` 讓「5 次/分鐘」實際上變成 20 次/分鐘。
  - **壞掉時怎麼表現** —— slowapi 對自己的 logger 掛了 `BlackHoleHandler`,退化無聲。

合併掉之後,這三件只剩一個地方要想對。

固定窗在窗邊界可以爆到約 2 倍,對「防捶」這種粗粒度用途沒問題(不適合精確配額)。
"""
import re

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from app.core.config import get_settings
from app.core.exceptions import RateLimited

_PERIODS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(second|minute|hour|day)\s*$")


def parse_rate(spec: str) -> tuple[int, int]:
    """把 `"5/minute"` 解析成 `(5, 60)`。

    保留字串形式是因為它在 `.env` 裡讀得懂,而且沿用了 slowapi 時代的慣例 ——
    設定檔不必為了一次內部重構而改寫。
    """
    match = _RATE_RE.match(spec)
    if not match:
        raise ValueError(f"bad rate limit spec {spec!r}; expected e.g. '5/minute'")
    return int(match.group(1)), _PERIODS[match.group(2)]


# INCR 之後只在「這是窗內第一筆」時設 TTL。兩個指令必須原子:分開送的話,process 在
# 兩者之間死掉會留下一把**永不過期**的計數器 —— 那個 key 從此卡在上限,對應的 IP 或
# 帳號就永久被鎖住,而且沒有任何東西會把它清掉。
_HIT_LUA = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return {n, redis.call('TTL', KEYS[1])}
"""

_hit_script: AsyncScript | None = None


def _full_key(key: str) -> str:
    return f"rl:{key}"


async def hit(redis: Redis, key: str, *, window_seconds: int) -> tuple[int, int]:
    """記一次,回傳 (窗內累計次數, 剩餘秒數)。"""
    global _hit_script
    if _hit_script is None:
        _hit_script = redis.register_script(_HIT_LUA)
    count, ttl = await _hit_script(
        keys=[_full_key(key)], args=[window_seconds], client=redis
    )
    return int(count), int(ttl)


async def peek(redis: Redis, key: str) -> tuple[int, int]:
    """讀但不記。回傳 (次數, 剩餘秒數);key 不存在時是 (0, 0)。

    登入的每帳號鎖定需要它:那個計數器只能由**失敗**推進,所以「檢查」與「記一次」
    必須拆成兩件事。用 `enforce_rate_limit` 的話,每一次成功登入也會把自己的帳號
    往鎖定推一格。
    """
    full = _full_key(key)
    async with redis.pipeline(transaction=False) as pipe:
        pipe.get(full)
        pipe.ttl(full)
        raw, ttl = await pipe.execute()
    return (int(raw) if raw is not None else 0), max(0, int(ttl))


async def clear(redis: Redis, key: str) -> None:
    """清掉計數。成功登入之後用它把該帳號的失敗次數歸零。"""
    await redis.delete(_full_key(key))


async def enforce_rate_limit(
    redis: Redis, key: str, *, limit: int, window_seconds: int
) -> None:
    """記一次;超過 `limit` 就拋 RateLimited(→ 429 + Retry-After)。

    測試環境用 `RATE_LIMIT_ENABLED=False` 整組關掉 —— 大多數測試會重複登入/註冊,
    不關的話它們會為了跟自己想測的東西完全無關的理由變紅。要驗限流本身的測試自己
    把它打開(見 test_first_minute_hardening.py)。
    """
    if not get_settings().RATE_LIMIT_ENABLED:
        return
    count, ttl = await hit(redis, key, window_seconds=window_seconds)
    if count > limit:
        raise RateLimited(retry_after=ttl if ttl > 0 else window_seconds)


async def enforce(redis: Redis, key: str, *, rate: str) -> None:
    """`enforce_rate_limit` 的字串版:`await enforce(redis, key, rate="5/minute")`。"""
    limit, window = parse_rate(rate)
    await enforce_rate_limit(redis, key, limit=limit, window_seconds=window)
