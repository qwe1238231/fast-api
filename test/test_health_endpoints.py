"""健康檢查的兩層。

**最重要的一條是 `test_the_alb_probe_touches_no_dependency`** —— 它守的是一個看起來
像改良的改動:「既然要健康檢查,不如順便查一下 DB」。那個改動會把一次資料庫抖動放大
成全面停機:DB 是共用的,所以深度檢查會讓**每一個** target 同時 unhealthy,ALB 沒有
後端可送(對使用者是完全中斷),而 ECS 把所有任務殺掉重啟、重啟再去捶正在恢復的資料庫。
任務重啟治得好「這個 process 壞了」,治不好「大家共用的東西壞了」。
"""
import asyncio

import pytest

from app.main import app


@pytest.mark.asyncio
async def test_the_liveness_probe_answers_200(client) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_the_alb_probe_touches_no_dependency(client, monkeypatch) -> None:
    """`/health` 在 DB 與 Redis 都不通的時候仍然要回 200。

    這不是寬鬆,是刻意的分工:ALB 的檢查決定「要不要殺掉重啟這個任務」,而共用依賴
    掛掉的時候重啟一點忙都幫不上,只會讓恢復更慢。「活著但服務不了」由 ALB 的
    HTTPCode_Target_5XX_Count 告警負責 —— 真實流量上的數值訊號。
    """
    async def explode(*args, **kwargs):
        raise RuntimeError("dependency is down")

    # 把兩個依賴都打壞。如果 /health 有碰它們,這裡就會變成 500 或 503。
    monkeypatch.setattr("app.main.text", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("should not be called by /health")
    ))
    monkeypatch.setattr(app.state.redis, "ping", explode)

    r = await client.get("/health")
    assert r.status_code == 200, (
        "/health 碰到依賴了 —— 那會讓資料庫一抖就殺光所有任務"
    )


@pytest.mark.asyncio
async def test_the_deep_probe_reports_both_dependencies(client) -> None:
    r = await client.get("/health/deps")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"postgres", "redis"}, "兩個依賴都要被查"
    assert all(v == "ok" for v in body["checks"].values())


@pytest.mark.asyncio
async def test_the_deep_probe_is_503_when_redis_is_down(client, monkeypatch) -> None:
    """回 503 而不是 500:這是「我知道自己不健康」,不是「我壞了」。

    deploy.yml 的部署後煙霧測試靠這個狀態碼判斷要不要讓部署失敗。
    """
    async def explode(*args, **kwargs):
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(app.state.redis, "ping", explode)

    r = await client.get("/health/deps")
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"] == "ok", "只有 redis 壞,postgres 不該被連坐"
    assert "ConnectionError" in body["checks"]["redis"]


@pytest.mark.asyncio
async def test_a_hanging_dependency_does_not_hang_the_probe(client, monkeypatch) -> None:
    """卡住的依賴必須在逾時後被判定為壞,不能讓探測自己也卡住。

    探測卡住的話,「壞了」跟「還在查」變得無法區分 —— 而那正是健康檢查最不該有的
    性質:呼叫端(值班的人、CI)只會看到一個沒有回應的請求,得不到任何資訊。
    """
    async def hang(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(app.state.redis, "ping", hang)
    monkeypatch.setattr("app.main._DEPS_PROBE_TIMEOUT_SECONDS", 0.1)

    r = await asyncio.wait_for(client.get("/health/deps"), timeout=10)
    assert r.status_code == 503
    assert "TimeoutError" in r.json()["checks"]["redis"]


def test_the_liveness_probe_is_not_in_the_public_schema() -> None:
    """健康檢查是給基礎設施用的,不是 API 契約 —— 別出現在 /docs 上讓人以為可以依賴它。"""
    paths = app.openapi()["paths"]
    assert "/health" not in paths
    assert "/health/deps" not in paths
