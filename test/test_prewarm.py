"""開賣預熱訊號 —— `sale_imminent` 的窗計算與發佈。

這個訊號存在的理由:搶票的尖峰是**秒級**的,而 ECS 拉起一個新任務是**分鐘級**的。
等 CPU 高了才擴,尖峰已經過去了(過去的方式是使用者吃 503)。所以擴容必須在已知的
開賣時刻**之前**發生,而開賣時刻只存在資料庫裡 —— Terraform 看不到它,只能由 worker
每分鐘把它變成一個 CloudWatch 指標。

窗的定義:`[sale_starts_at − PREWARM_LEAD_MINUTES, sale_starts_at + PREWARM_TAIL_MINUTES]`
"""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.models.event import Event, EventStatus
from app.worker import METRIC_NAME_SALE_IMMINENT, find_imminent_sale, publish_prewarm_signal

LEAD = timedelta(minutes=20)
TAIL = timedelta(minutes=15)


async def _event(db, *, sale_starts_at: datetime, status=EventStatus.PUBLISHED) -> Event:
    event = Event(
        name="Prewarm Test", venue="Test Arena",
        starts_at=sale_starts_at + timedelta(days=30),
        ends_at=sale_starts_at + timedelta(days=30, hours=3),
        sale_starts_at=sale_starts_at,
        sale_ends_at=sale_starts_at + timedelta(days=1),
        total_seats=100, price_cents=1500, status=status,
    )
    db.add(event)
    await db.commit()
    return event


# ─ 窗的邊界

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(minutes=-10), True),   # 開賣前 10 分 —— 窗內(lead 是 20 分)
        (timedelta(0), True),             # 正好開賣
        (timedelta(minutes=10), True),    # 開賣後 10 分 —— 還在 tail 內
        (timedelta(minutes=-40), False),  # 開賣前 40 分 —— 還太早,別浪費容量
        (timedelta(minutes=40), False),   # 開賣後 40 分 —— 尖峰過了,可以縮
    ],
    ids=["10m before", "at sale", "10m after", "40m before", "40m after"],
)
async def test_window_boundaries(db, offset, expected):
    now = datetime.now(timezone.utc)
    await _event(db, sale_starts_at=now + offset)

    found = await find_imminent_sale(db, now=now, lead=LEAD, tail=TAIL)

    assert (found is not None) is expected


@pytest.mark.asyncio
async def test_unpublished_events_do_not_trigger_prewarm(db):
    """草稿場次不會有人來 —— 為它預熱是純粹的花錢。"""
    now = datetime.now(timezone.utc)
    await _event(db, sale_starts_at=now, status=EventStatus.DRAFT)

    assert await find_imminent_sale(db, now=now, lead=LEAD, tail=TAIL) is None


@pytest.mark.asyncio
async def test_the_nearest_imminent_sale_wins(db):
    """多場同時在窗內時回最近的那一個 —— 這個 id 只進 log,但它決定了值班的人第一個
    去看哪一場。"""
    now = datetime.now(timezone.utc)
    later = await _event(db, sale_starts_at=now + timedelta(minutes=15))
    sooner = await _event(db, sale_starts_at=now + timedelta(minutes=5))

    found = await find_imminent_sale(db, now=now, lead=LEAD, tail=TAIL)

    assert found == sooner.id != later.id


# ─ 發佈

@pytest.mark.asyncio
async def test_zero_is_published_too(db, monkeypatch):
    """**沒有開賣時也要發 0。**

    只在為真時發的話,平時完全沒有資料點,CloudWatch 的告警會停在 INSUFFICIENT_DATA
    而不是 OK —— 而 INSUFFICIENT_DATA 不會觸發任何動作,所以縮容那條路永遠不會走,
    容量就再也下不來了。跟 log metric filter 需要 `default_value = 0` 是同一件事。
    """
    published: list[dict] = []
    monkeypatch.setattr("app.worker._put_gauges", lambda values: _record(published, values))

    result = await publish_prewarm_signal({})

    assert result == 0
    assert published == [{METRIC_NAME_SALE_IMMINENT: 0}]


@pytest.mark.asyncio
async def test_one_is_published_inside_the_window(db, monkeypatch):
    published: list[dict] = []
    monkeypatch.setattr("app.worker._put_gauges", lambda values: _record(published, values))
    await _event(db, sale_starts_at=datetime.now(timezone.utc) + timedelta(minutes=5))

    result = await publish_prewarm_signal({})

    assert result == 1
    assert published == [{METRIC_NAME_SALE_IMMINENT: 1}]


async def _record(sink: list[dict], values: dict) -> None:
    sink.append(values)


# ─ 設定的守衛

def test_prewarm_must_start_before_the_waiting_room_opens():
    """預熱的提前量必須大於等候室的提前量。

    反過來的話,排隊一開放就有幾萬人掛上 SSE,而擴容還沒開始 —— 預熱這個機制存在的
    唯一理由就消失了,但設定看起來完全合理(兩個數字各自都很正常)。
    """
    base = get_settings().model_dump()

    with pytest.raises(ValidationError, match="must exceed"):
        Settings(**{**base, "PREWARM_LEAD_MINUTES": 5, "QUEUE_LEAD_TIME_SECONDS": 600})
