"""登入的每帳號失敗鎖定。

每 IP 限流擋不住有殭屍網路的人:每個 IP 都乖乖待在 5 次/分鐘以內,合起來仍然是每分鐘
幾千次猜測。要擋住那個,計數必須掛在**被攻擊的帳號**上,而不是掛在來源上。

這個檔案裡最重要的兩條,是那兩條在「加了鎖定」之後才**新出現**的風險:
  - 鎖定會不會變成帳號列舉的預言機(不存在的帳號表現不同)?
  - 鎖定會不會讓攻擊變得更便宜(被鎖了還照跑 Argon2)?
"""
from contextlib import contextmanager

import pytest

from app.core.config import get_settings
from app.api.v1.auth import _login_failure_key
from app.services.rate_limit import _full_key, peek

pytestmark = pytest.mark.asyncio

WRONG = {"password": "definitely-not-it"}


@contextmanager
def lockout_on():
    """conftest 預設關掉限流;這裡把它打開,並把每 IP 的桶調大,免得測試在撞到
    每帳號鎖定之前先被每 IP 限流擋掉 —— 那樣測到的就是另一件事。"""
    settings = get_settings()
    original = settings.LOGIN_RATE_LIMIT
    settings.LOGIN_RATE_LIMIT = "1000/minute"
    settings.RATE_LIMIT_ENABLED = True
    try:
        yield settings
    finally:
        settings.RATE_LIMIT_ENABLED = False
        settings.LOGIN_RATE_LIMIT = original


async def _register(client, username: str) -> None:
    r = await client.post(
        "/v1/users/", json={"username": username, "password": "secret123"}
    )
    assert r.status_code == 201, r.text


async def _login(client, username: str, password: str, **kwargs) -> int:
    r = await client.post(
        "/v1/auth/token", data={"username": username, "password": password}, **kwargs
    )
    return r.status_code


async def test_repeated_failures_lock_the_account(client) -> None:
    await _register(client, "target")
    with lockout_on() as settings:
        for _ in range(settings.LOGIN_FAILURE_LIMIT):
            assert await _login(client, "target", "wrong") == 401
        assert await _login(client, "target", "wrong") == 429


async def test_the_lock_follows_the_account_not_the_ip(client) -> None:
    """**這條是整個功能存在的理由。**

    攻擊者換 IP 是免費的(殭屍網路、住宅代理);換一個 IP 就重置計數的話,每 IP 限流
    等於白做。所以鎖定必須跟著帳號走。
    """
    await _register(client, "target")
    get_settings().TRUSTED_PROXY_COUNT = 1
    try:
        with lockout_on() as settings:
            for i in range(settings.LOGIN_FAILURE_LIMIT):
                # 每一次都換一個來源 IP —— 每 IP 的桶永遠是 1。
                assert await _login(
                    client, "target", "wrong",
                    headers={"X-Forwarded-For": f"203.0.113.{i}"},
                ) == 401
            assert await _login(
                client, "target", "wrong",
                headers={"X-Forwarded-For": "198.51.100.1"},
            ) == 429, "換 IP 就繞過的話,這個功能等於不存在"
    finally:
        get_settings().TRUSTED_PROXY_COUNT = 0


async def test_an_unknown_username_locks_the_same_way(client) -> None:
    """不存在的帳號必須表現得一模一樣,否則 401 與 429 的差異就是帳號列舉的預言機。

    計數的 key 用**送進來的字串**而不是查到的 user.id,就是為了這件事 —— 用 id 的話
    不存在的帳號根本沒有計數器,永遠回 401,而存在的帳號會轉成 429。攻擊者只要對
    每個候選名字打 11 次就能把整個使用者名單掃出來。
    """
    with lockout_on() as settings:
        for _ in range(settings.LOGIN_FAILURE_LIMIT):
            assert await _login(client, "ghost-account", "wrong") == 401
        assert await _login(client, "ghost-account", "wrong") == 429


async def test_case_variations_share_one_bucket(client) -> None:
    """`Target` / `TARGET` / `target` 是同一個桶 —— 否則大小寫排列就是免費的額度。"""
    await _register(client, "target")
    with lockout_on() as settings:
        for i in range(settings.LOGIN_FAILURE_LIMIT):
            name = "target".upper() if i % 2 else "TaRgEt"
            assert await _login(client, name, "wrong") == 401
        assert await _login(client, "target", "wrong") == 429


async def test_a_successful_login_clears_the_counter(client, redis) -> None:
    """打錯幾次最後打對的正常使用者,不能帶著一個逼近上限的計數器離開。

    不清零的話,他下次再打錯一兩次就被鎖 15 分鐘,而他從頭到尾沒做錯任何事。
    """
    await _register(client, "target")
    with lockout_on() as settings:
        for _ in range(settings.LOGIN_FAILURE_LIMIT - 1):
            assert await _login(client, "target", "wrong") == 401
        failures, _ = await peek(redis, _login_failure_key("target"))
        assert failures == settings.LOGIN_FAILURE_LIMIT - 1

        assert await _login(client, "target", "secret123") == 200
        assert await peek(redis, _login_failure_key("target")) == (0, 0)

        # 清乾淨了 → 又有完整的額度
        for _ in range(settings.LOGIN_FAILURE_LIMIT):
            assert await _login(client, "target", "wrong") == 401


async def test_a_locked_account_does_not_burn_cpu_on_hashing(
    client, monkeypatch
) -> None:
    """被鎖之後不能再跑 Argon2。

    這條擋的是「鎖定寫在驗密碼之後」那種寫法:功能看起來一樣正確(還是回 429),
    但對單一帳號狂捶仍然每次燒掉 ~220ms 的 CPU —— 限流器反而在幫攻擊加熱。
    """
    import app.api.v1.auth as auth

    await _register(client, "target")
    with lockout_on() as settings:
        for _ in range(settings.LOGIN_FAILURE_LIMIT):
            await _login(client, "target", "wrong")

        calls = 0

        def counting_verify(*args, **kwargs):
            nonlocal calls
            calls += 1
            return False

        monkeypatch.setattr(auth, "verify_password", counting_verify)
        monkeypatch.setattr(auth, "constant_time_dummy_verify", counting_verify)

        assert await _login(client, "target", "wrong") == 429
        assert calls == 0, "被鎖的請求不該再跑一次雜湊"


async def test_the_429_says_when_to_come_back(client) -> None:
    """回 Retry-After。少了它,客戶端只能瞎猜,而寫得差的客戶端會立刻重送 —— 那正是
    我們要擋掉的流量。"""
    await _register(client, "target")
    with lockout_on() as settings:
        for _ in range(settings.LOGIN_FAILURE_LIMIT):
            await _login(client, "target", "wrong")
        blocked = await client.post(
            "/v1/auth/token", data={"username": "target", "password": "wrong"}
        )
    assert blocked.status_code == 429
    retry_after = int(blocked.headers["Retry-After"])
    assert 0 < retry_after <= get_settings().LOGIN_FAILURE_WINDOW_SECONDS


async def test_the_failure_counter_expires_by_itself(client, redis) -> None:
    """計數器一定要有 TTL,而且 TTL 必須跟 INCR 原子。

    **這是這個功能最危險的失敗模式**:沒有 TTL 的話那把 key 永遠留著,帳號從此
    鎖死,而且沒有任何東西會清掉它 —— 使用者的唯一救濟是有人去 Redis 手動 DEL。
    分開送 INCR 與 EXPIRE 也一樣壞:process 在兩者之間死掉就留下一把不會過期的 key,
    而那個機率隨流量線性上升。

    整套裡只有這一條抓得到:把 Lua 的 EXPIRE 拿掉,其餘 362 條全綠。
    """
    await _register(client, "target")
    with lockout_on() as settings:
        assert await _login(client, "target", "wrong") == 401

    ttl = await redis.ttl(_full_key(_login_failure_key("target")))
    assert 0 < ttl <= settings.LOGIN_FAILURE_WINDOW_SECONDS, (
        f"失敗計數的 TTL 是 {ttl} —— -1 代表永不過期,那個帳號被永久鎖死"
    )


async def test_locking_one_account_does_not_touch_another(client) -> None:
    """代價是有界的:攻擊者能把某個人擋在門外 15 分鐘,但擋不到別人。"""
    await _register(client, "target")
    await _register(client, "bystander")
    with lockout_on() as settings:
        for _ in range(settings.LOGIN_FAILURE_LIMIT + 1):
            await _login(client, "target", "wrong")
        assert await _login(client, "bystander", "secret123") == 200
