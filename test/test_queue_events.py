"""Waiting-room live-push machinery: the admit-deadline math, the in-process
Pub/Sub fan-out, and the review fixes for the SSE upgrade (best-effort pokes,
release() sentinel, 0-crossing pokes)."""
from collections import defaultdict
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import get_settings
from app.services import queue_events as qe
from app.services import waiting_room as wr
from app.services.inventory import release, reserve, _key


def _poke_spy(monkeypatch) -> list[int]:
    """Replace inventory.publish_event_poke with an async spy; return the list of
    event_ids it was poked with."""
    calls: list[int] = []

    async def spy(_redis, event_id):
        calls.append(event_id)

    monkeypatch.setattr("app.services.inventory.publish_event_poke", spy)
    return calls


# ---------- admit_deadline: the "sleep precisely until admission" math ----------

@pytest.mark.asyncio
async def test_admit_deadline_matches_admission_formula(redis, published_event):
    eid = published_event.id
    await redis.zadd(wr._draw_key(eid), {"1": 0.0, "2": 1.0})   # user 1 rank 0, user 2 rank 1
    await redis.set(wr._admit_start_key(eid), 1000.0)
    rate = get_settings().QUEUE_ADMISSION_RATE

    assert await wr.admit_deadline(redis, event_id=eid, user_id=1) == pytest.approx(1000.0 + 1 / rate)
    assert await wr.admit_deadline(redis, event_id=eid, user_id=2) == pytest.approx(1000.0 + 2 / rate)


@pytest.mark.asyncio
async def test_admit_deadline_none_when_unscheduled_or_unregistered(redis, published_event):
    eid = published_event.id
    await redis.zadd(wr._draw_key(eid), {"1": 0.0})
    assert await wr.admit_deadline(redis, event_id=eid, user_id=1) is None   # no admit_start yet
    await redis.set(wr._admit_start_key(eid), 1000.0)
    assert await wr.admit_deadline(redis, event_id=eid, user_id=999) is None  # not registered


# ---------- in-process fan-out: register / wake / coalesce / unregister ----------

@pytest.mark.asyncio
async def test_register_wake_coalesce_unregister(monkeypatch):
    monkeypatch.setattr(qe, "_mailboxes", defaultdict(set))
    m1, m2 = qe.register(7), qe.register(7)

    qe._wake(7)
    assert m1.qsize() == 1 and m2.qsize() == 1     # both nudged
    qe._wake(7)                                     # maxsize=1 -> coalesced, not stacked
    assert m1.qsize() == 1

    qe.unregister(7, m1)
    qe.unregister(7, m2)
    assert 7 not in qe._mailboxes                   # empty bucket cleaned up


@pytest.mark.asyncio
async def test_wake_all_spans_every_event(monkeypatch):
    monkeypatch.setattr(qe, "_mailboxes", defaultdict(set))
    a, b = qe.register(1), qe.register(2)
    qe._wake_all()                                  # global pause poke path
    assert a.qsize() == 1 and b.qsize() == 1


def test_event_id_from_channel():
    assert qe._event_id_from_channel("queue:42:events") == 42
    assert qe._event_id_from_channel("queue:all:events") is None   # global -> routed to _wake_all
    assert qe._event_id_from_channel("garbage") is None


# ---------- Fix B: pokes are best-effort — never fail the caller ----------

@pytest.mark.asyncio
async def test_publish_pokes_swallow_errors():
    down = MagicMock()
    down.publish = AsyncMock(side_effect=RuntimeError("redis down"))
    await qe.publish_event_poke(down, 1)            # must not raise
    await qe.publish_global_poke(down)              # must not raise
    assert down.publish.await_count == 2


# ---------- Fix C: release() sentinel no longer collides with a -1 stock ----------

@pytest.mark.asyncio
async def test_release_reports_true_when_new_count_is_negative(redis, published_event):
    """Oversell recovery can leave stock negative; a genuine release whose new
    count lands on -1 must report True, not be misread as a replay (DUP)."""
    eid = published_event.id
    await redis.set(_key(eid), -2)                  # oversold — reconcile writes negatives
    assert await release(redis, event_id=eid, quantity=1, marker=f"order:{eid}:x") is True
    assert int(await redis.get(_key(eid))) == -1    # seat actually returned


@pytest.mark.asyncio
async def test_release_is_idempotent_per_marker(redis, published_event):
    eid = published_event.id
    await redis.set(_key(eid), 0)
    m = f"order:{eid}:y"
    assert await release(redis, event_id=eid, quantity=1, marker=m) is True
    assert await release(redis, event_id=eid, quantity=1, marker=m) is False   # replay -> DUP
    assert int(await redis.get(_key(eid))) == 1     # returned exactly once


# ---------- stage 2: pokes fire on a 0-crossing, and only then ----------

@pytest.mark.asyncio
async def test_reserve_pokes_only_on_sold_out_crossing(redis, published_event, monkeypatch):
    eid = published_event.id
    await redis.set(_key(eid), 2)
    calls = _poke_spy(monkeypatch)

    await reserve(redis, event_id=eid, quantity=1)   # 2 -> 1: no crossing
    assert calls == []
    await reserve(redis, event_id=eid, quantity=1)   # 1 -> 0: sold-out crossing
    assert calls == [eid]


@pytest.mark.asyncio
async def test_reserve_and_enqueue_pokes_on_sold_out_crossing(redis, published_event, monkeypatch):
    from app.services.inventory import reserve_and_enqueue, ReserveOutcome
    eid = published_event.id
    await redis.set(_key(eid), 1)
    calls = _poke_spy(monkeypatch)

    res = await reserve_and_enqueue(                 # takes the last seat -> Lua returns remaining==0
        redis, event_id=eid, user_id=1, quantity=1,
        total_price_cents=1500, idempotency_key=str(uuid4()),
    )
    assert res.outcome == ReserveOutcome.OK
    assert calls == [eid]


@pytest.mark.asyncio
async def test_release_pokes_only_on_restock_crossing(redis, published_event, monkeypatch):
    eid = published_event.id
    await redis.set(_key(eid), 0)
    calls = _poke_spy(monkeypatch)

    await release(redis, event_id=eid, quantity=1, marker=f"order:{eid}:a")   # 0 -> 1: restock crossing
    assert calls == [eid]
    await release(redis, event_id=eid, quantity=1, marker=f"order:{eid}:b")   # 1 -> 2: no crossing
    assert calls == [eid]
