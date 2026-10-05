"""開賣第一分鐘必然會撞到的五件事。

放在同一個檔案是因為它們有同一個成因:**每一個都是「原則只在當初寫下它的那個檔案裡
被落實」**。連線的生命週期、token 的 subject、真實客戶端 IP、CPU 密集工作的位置、
串流的長度上限 —— 每一項在別處都做對過一次,然後在下一個檔案裡從頭來過。

也放在同一個檔案是因為它們都**不會在正常測試裡自己現形**:五件裡有四件在單機、
低併發、沒有反向代理的環境下行為完全正常。要嘛靠結構斷言,要嘛靠刻意製造併發。
"""
import asyncio

import pytest
from starlette.requests import Request

from app.api.deps import client_ip, get_stream_user
from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.session import get_db
from app.main import app
from app.services.inventory import ORDER_DEAD_LETTER_KEY
from app.services.rate_limit import _full_key, parse_rate
from app.worker import DEAD_LETTER_CURSOR_KEY, count_new_dead_letters


# ─ 1) SSE 不得抓著共用的 DB 連線

def _dependency_calls(dependant) -> set:
    """把一個路由的依賴樹攤平成「會被呼叫的 callable」集合。"""
    found = {dependant.call}
    for sub in dependant.dependencies:
        found |= _dependency_calls(sub)
    return found


def test_the_sse_route_does_not_hold_the_shared_db_session() -> None:
    """SSE 路由的依賴樹裡不能有 `get_db`。

    FastAPI 的 yield 依賴要活到**回應結束**才收掉,而 SSE 的回應要到生成器跑完
    (最長 300 秒)才算結束。所以 `db: DbSession` 出現在這條路由上的意思是:每一條
    串流佔住一條 DB 連線五分鐘。DB_POOL_SIZE=5 + DB_MAX_OVERFLOW=10 → 每個 process
    只能有 15 條並發 SSE —— 而等候室正是「幾萬人同時連著」的地方。

    用結構斷言而不是壓力測試,是因為這個回歸的形狀就是結構性的:有人為了拿個 user
    順手加一個 `db: DbSession`,單機測試完全正常。
    """
    route = next(
        r for r in app.routes
        if getattr(r, "path", None) == "/v1/events/{event_id}/queue/stream"
    )
    calls = _dependency_calls(route.dependant)
    assert get_stream_user in calls, "前提:這條路由真的走 get_stream_user"
    assert get_db not in calls, "SSE 不能吃共用的 DbSession —— 它會被握住整條串流"


@pytest.mark.asyncio
async def test_stream_auth_returns_its_connection_before_the_stream_starts(
    client
) -> None:
    """認證做完之後,借出的連線必須已經還回去,而且拿到的 user 是脫離狀態。

    量的是因果鏈的正中間:不是「N 條並發會不會爆」(那要看池子大小、逾時、機器負載,
    會 flaky),而是「這個依賴回來的時候手上還有沒有連線」。有就是 1,沒有就是 0。

    不從 HTTP 那一層測是有原因的:`ASGITransport` 會把整個回應**緩衝到 app 結束**
    才回傳(見 httpx `_transports/asgi.py` 的 `body_parts`),所以測試 client 根本
    看不到一條「開著的」串流 —— 那是測試傳輸層的限制,不是實作的性質。真正要驗證
    整條路徑得起一個真的 uvicorn,不值得放進單元測試套件。

    `detached` 那一條同樣重要:如果 user 還黏在已關閉的 session 上,串流中途任何一個
    lazy load 都會安靜地再借一條連線回來,把修好的東西又漏掉。
    """
    from sqlalchemy import inspect as sa_inspect
    from app.db.session import engine

    await client.post("/v1/users/", json={"username": "ssewatch", "password": "secret123"})
    token = (await client.post(
        "/v1/auth/token", data={"username": "ssewatch", "password": "secret123"}
    )).json()["access_token"]

    baseline = engine.pool.checkedout()
    user = await get_stream_user(header_token=token)

    assert user.username == "ssewatch"
    assert engine.pool.checkedout() == baseline, "認證回來之後不能還握著連線"
    assert sa_inspect(user).detached, "user 必須脫離 session,否則 lazy load 會再借一條"


# ─ 2) access token 的 subject

@pytest.mark.asyncio
async def test_a_refreshed_access_token_still_authenticates(client) -> None:
    """**這個 bug 的實際症狀是「每一張刷新過的 token 都 401」。**

    `/auth/token` 以前發 `sub=username`、`/auth/refresh` 發 `sub=str(user_id)`,而
    `deps` 只用 username 查 —— 所以使用者只要在站上待超過一個 access token 的壽命,
    下一個請求就掉登入。既有的 refresh 測試只斷言回應裡「有 access_token」,從來
    沒有拿它去用過,所以測得再多也抓不到。
    """
    await client.post("/v1/users/", json={"username": "rot", "password": "secret123"})
    logged_in = await client.post(
        "/v1/auth/token", data={"username": "rot", "password": "secret123"}
    )
    assert logged_in.status_code == 200

    refreshed = await client.post(
        "/v1/auth/refresh", headers={"X-CSRF-Token": client.cookies.get("csrf_token")}
    )
    assert refreshed.status_code == 200
    new_access = refreshed.json()["access_token"]

    me = await client.get(
        "/v1/users/me", headers={"Authorization": f"Bearer {new_access}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "rot"


@pytest.mark.asyncio
async def test_a_numeric_username_cannot_impersonate_that_user_id(client, db) -> None:
    """帳號名叫「1」的人不能收下 user_id=1 的請求。

    這是 username 與 user_id 共用一個 `sub` 欄位的必然結果:只要查詢是用 username
    做的,一個註冊了純數字帳號名的人就會接收所有 id 等於那個數字的人的請求 ——
    包含他們的訂單與 PII。改成一律用 id 查之後,數字帳號名就只是個普通名字。
    """
    from sqlalchemy import select
    from app.models.user import User

    from app.core.security import get_password_hash

    # 受害者的 id 指定成 100:註冊 API 的 username min_length=3 讓 id 1~99 撞不到,
    # 但 100 以上完全撞得到 —— 而那個長度限制是為了別的理由存在的,不該被當成
    # 這個漏洞的防線。
    victim = User(id=100, username="victim", hashed_password=get_password_hash("secret123"))
    db.add(victim)
    await db.commit()

    # 冒名者把自己的**帳號名**取成受害者的 **id**。
    registered = await client.post(
        "/v1/users/", json={"username": "100", "password": "secret123"}
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["id"] != 100, "前提:兩個是不同的人"

    # 受害者刷新 token(refresh 一向把 sub 設成 user_id),再拿新 token 去用。
    # 舊實作在這裡用 sub="100" 去查 **username**,查到的是冒名者 —— 於是受害者的
    # 整個工作階段變成冒名者的帳號,他接下來填的付款資料與 PII 都進了對方的戶頭。
    await client.post("/v1/auth/token", data={"username": "victim", "password": "secret123"})
    refreshed = await client.post(
        "/v1/auth/refresh", headers={"X-CSRF-Token": client.cookies.get("csrf_token")}
    )
    assert refreshed.status_code == 200, refreshed.text

    me = await client.get(
        "/v1/users/me",
        headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "victim", "刷新後仍然必須是受害者本人"


@pytest.mark.asyncio
async def test_a_token_whose_subject_is_a_username_is_rejected(client) -> None:
    """舊格式(sub=username)的 token 一律 401,不做相容。

    相容的寫法是「數字就查 id、否則查 username」,而那正好把上面那個冒名漏洞留著。
    access token 是分鐘級的,部署時最多一個 TTL 的抖動,客戶端拿 refresh cookie
    換一張就好。
    """
    await client.post("/v1/users/", json={"username": "legacy", "password": "secret123"})
    stale = create_access_token(subject="legacy")

    me = await client.get("/v1/users/me", headers={"Authorization": f"Bearer {stale}"})
    assert me.status_code == 401


# ─ 3) 真實客戶端 IP

def _request(headers: dict[str, str], peer: str = "10.0.0.1") -> Request:
    return Request({
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 12345),
    })


def test_xff_is_ignored_when_no_proxy_is_declared(monkeypatch) -> None:
    """預設不信任 XFF。設錯成 0 只是限流變嚴;設錯成 1 而前面沒有代理,就是誰都
    能自稱任意 IP 而完全繞過限流。"""
    monkeypatch.setattr(get_settings(), "TRUSTED_PROXY_COUNT", 0)
    assert client_ip(_request({"X-Forwarded-For": "1.2.3.4"})) == "10.0.0.1"


def test_one_proxy_takes_the_rightmost_hop(monkeypatch) -> None:
    """ALB 是**附加**在既有 XFF 後面,所以最右邊那個才是它親眼看到的對端。

    取最左邊(常見寫法)等於直接採信客戶端塞進來的值。這條測試餵的就是那個攻擊:
    客戶端自稱 9.9.9.9,ALB 把真實的 5.6.7.8 接在後面。
    """
    monkeypatch.setattr(get_settings(), "TRUSTED_PROXY_COUNT", 1)
    spoofed = _request({"X-Forwarded-For": "9.9.9.9, 5.6.7.8"})
    assert client_ip(spoofed) == "5.6.7.8"


def test_two_proxies_skip_two_hops(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "TRUSTED_PROXY_COUNT", 2)
    req = _request({"X-Forwarded-For": "9.9.9.9, 1.1.1.1, 2.2.2.2"})
    assert client_ip(req) == "1.1.1.1"


def test_a_shorter_chain_than_declared_falls_back_inside_the_chain(monkeypatch) -> None:
    """宣告 2 層但只來了 1 跳(例如健康檢查直打 ALB 目標群組)不能越界取到不存在
    的索引,更不能拋例外把請求變成 500。"""
    monkeypatch.setattr(get_settings(), "TRUSTED_PROXY_COUNT", 2)
    assert client_ip(_request({"X-Forwarded-For": "5.6.7.8"})) == "5.6.7.8"


@pytest.mark.asyncio
async def test_the_limiter_buckets_by_the_real_client_not_the_proxy(
    client, monkeypatch, rate_limiting
) -> None:
    """限流器真的**接上** `client_ip`,而且偽造的 XFF 分不到新的桶。

    上面那幾條測的是 `client_ip` 自己算得對不對;這條測的是它有沒有被接到限流器上。
    少了這條,把 `key_func` 改回 `get_remote_address` 只會讓一條測試變紅(儲存那條),
    而「全站共用一個桶」這個實際後果沒有任何測試看得到。
    """
    monkeypatch.setattr(get_settings(), "TRUSTED_PROXY_COUNT", 1)
    cap, _ = parse_rate(get_settings().REGISTER_RATE_LIMIT)

    async def register(n: int, xff: str) -> int:
        r = await client.post(
            "/v1/users/",
            json={"username": f"ip{n}", "password": "secret123"},
            headers={"X-Forwarded-For": xff},
        )
        return r.status_code

    # 客戶端 A(ALB 看到 5.6.7.8)把自己的桶用完。
    for n in range(cap):
        assert await register(n, "5.6.7.8") == 201
    assert await register(900, "5.6.7.8") == 429

    # 同一個人換一個**偽造的**左邊欄位 —— ALB 附加的仍然是 5.6.7.8,所以同一個桶。
    assert await register(901, "1.1.1.1, 5.6.7.8") == 429, "偽造 XFF 不該換到新桶"

    # 真的換一台機器(ALB 看到 9.9.9.9)→ 新的桶。
    assert await register(902, "9.9.9.9") == 201


@pytest.mark.asyncio
async def test_the_counter_lives_in_redis_not_in_this_process(
    client, redis, rate_limiting
) -> None:
    """限流計數器必須看得見在 Redis 裡。

    在 process 記憶體裡的話,api 跑 `--workers 4`、ECS 又有多個 task,「5 次/分鐘」
    就變成 5 × process 數,而且一重啟就歸零 —— 攻擊者只要等一次部署。
    斷言「Redis 裡真的有那把 key」而不是「儲存後端的類別名不含 memory」:前者不管
    換成哪一套實作都成立。
    """
    await client.post(
        "/v1/users/", json={"username": "redischeck", "password": "secret123"}
    )
    keys = [k for k in await redis.keys(_full_key("register:*"))]
    assert keys, "限流計數沒有出現在 Redis —— 它一定是留在 process 記憶體裡"


# ─ 4) 註冊:無認證 + 寫 DB + 一次 Argon2

@pytest.mark.asyncio
async def test_registration_is_rate_limited(client, rate_limiting) -> None:
    """註冊是無認證端點,每次呼叫寫一列 DB 並跑一次 ~220ms 的 Argon2 —— 不擋的話
    任何人都能免費徵用 CPU 與連線池,而搶票站開賣前本來就會被註冊機器人打。"""
    cap, _ = parse_rate(get_settings().REGISTER_RATE_LIMIT)
    codes = [
        (await client.post(
            "/v1/users/", json={"username": f"flood{i}", "password": "secret123"}
        )).status_code
        for i in range(cap + 1)
    ]
    assert codes[:cap] == [201] * cap
    assert codes[-1] == 429, "超過上限之後必須被擋"


@pytest.mark.asyncio
async def test_registration_does_not_block_the_event_loop(client, monkeypatch) -> None:
    """註冊進行中,event loop 必須還能跑別的東西。

    Argon2 是純 CPU 的 ~220ms。在 async 端點裡直接呼叫同步版本的話,那 220ms 之內
    **整個 worker process 的所有請求**都停住 —— 開賣瞬間別人的下單、SSE 心跳、
    健康檢查全部跟著卡。登入路徑早就用 threadpool 了,註冊卻沒有。

    conftest 把 Argon2 換成了 SHA-256 替身(快到量不出差別),所以這裡再換成一個
    真的會擋住執行緒的替身來重現那 220ms —— 否則這條測試不管有沒有 offload 都會綠,
    只是在量 SHA-256 有多快。
    """
    import time
    import ticket_secrets

    monkeypatch.setattr(
        ticket_secrets, "hash_password", lambda pw: (time.sleep(0.3), "faketest$slow")[1]
    )

    ticks = 0
    done = asyncio.Event()

    async def register() -> int:
        try:
            r = await client.post(
                "/v1/users/", json={"username": "slowreg", "password": "secret123"}
            )
            return r.status_code
        finally:
            done.set()

    async def tick() -> None:
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    status_code, _ = await asyncio.gather(register(), tick())
    assert status_code == 201
    # 同步雜湊會把 loop 整整凍住 0.3 秒 → ticks 幾乎是 0。丟到執行緒則約 30 次。
    assert ticks >= 10, f"註冊把 event loop 凍住了(只跑了 {ticks} 次)"


# ─ 5) 死信串流:有界,而且斷路器吃增量

@pytest.mark.asyncio
async def test_the_first_check_reports_zero_and_aligns_the_cursor(redis) -> None:
    """worker 重啟後的第一次檢查必須回 0。

    不對齊游標的話,每次重啟都會把整段歷史算成「剛剛新增的」而立刻跳閘 —— 那就是
    我們要修掉的那個 bug 換一個入口回來。
    """
    for i in range(5):
        await redis.xadd(ORDER_DEAD_LETTER_KEY, {"idempotency_key": str(i)})

    assert await count_new_dead_letters(redis, cap=100) == 0
    assert await redis.get(DEAD_LETTER_CURSOR_KEY) is not None


@pytest.mark.asyncio
async def test_only_entries_added_since_the_last_check_are_counted(redis) -> None:
    await count_new_dead_letters(redis, cap=100)          # 對齊
    for i in range(3):
        await redis.xadd(ORDER_DEAD_LETTER_KEY, {"idempotency_key": str(i)})

    assert await count_new_dead_letters(redis, cap=100) == 3
    assert await count_new_dead_letters(redis, cap=100) == 0, "同一批不能被數第二次"


@pytest.mark.asyncio
async def test_a_burst_does_not_keep_the_breaker_open_afterwards(redis) -> None:
    """一次湧入遠超過門檻的量之後,下一次檢查必須回 0。

    游標只推進到「我們數到的那 cap+1 筆」的話,一萬筆的事故會讓斷路器在事故結束後
    又多開一百分鐘 —— 同一個永久停售的病,只是慢一點。所以游標一律推進到當下最新。
    """
    await count_new_dead_letters(redis, cap=10)
    for i in range(50):
        await redis.xadd(ORDER_DEAD_LETTER_KEY, {"idempotency_key": str(i)})

    assert await count_new_dead_letters(redis, cap=10) == 11, "數到 cap+1 就夠了"
    assert await count_new_dead_letters(redis, cap=10) == 0, "事故結束就要放行"


@pytest.mark.asyncio
async def test_the_total_depth_alone_would_latch_forever(redis) -> None:
    """把舊行為寫成測試,說明為什麼要改。

    `XLEN` 只增不減 —— 沒有任何路徑會刪死信。所以拿它比門檻的斷路器,在第一次事故
    之後就永遠開著,整站永久停售,而且外觀跟「系統正在保護自己」一模一樣。
    """
    for i in range(5):
        await redis.xadd(ORDER_DEAD_LETTER_KEY, {"idempotency_key": str(i)})
    depth_after_incident = await redis.xlen(ORDER_DEAD_LETTER_KEY)

    await count_new_dead_letters(redis, cap=100)          # 事故過去了
    assert await count_new_dead_letters(redis, cap=100) == 0   # 增量:恢復
    assert await redis.xlen(ORDER_DEAD_LETTER_KEY) == depth_after_incident  # 總量:不會降


@pytest.mark.asyncio
async def test_dead_lettering_bounds_the_stream(redis, monkeypatch) -> None:
    """死信 XADD 必須帶 maxlen(對照 audit.py 的同一個模式)。

    不帶的話它只增不減,而每一筆都留著完整的 order intent 欄位:一次持續的下游故障
    就能把 Redis 撐爆 —— 而 Redis 同時是庫存的唯一真相來源,於是「訂單寫不進 DB」
    會升級成「整站賣不了票」。
    """
    from uuid import uuid4
    import app.worker as worker

    seen: dict = {}
    original = redis.xadd

    async def spy(key, fields, **kwargs):
        if key == ORDER_DEAD_LETTER_KEY:
            seen.update(kwargs)
        return await original(key, fields, **kwargs)

    monkeypatch.setattr(redis, "xadd", spy)
    await worker._dead_letter_intent(
        redis, "1-1", {"idempotency_key": str(uuid4()), "event_id": "1", "quantity": "1",
                       "user_id": "1"},
    )
    assert seen.get("maxlen") == worker.ORDER_DEAD_LETTER_MAX_LEN
    assert seen.get("approximate") is True, "近似修剪:精確修剪要掃整條 stream"
