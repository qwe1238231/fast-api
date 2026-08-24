"""個資抹除:匿名化 + crypto-shred,而訂單活下來。

這個檔案要證明的是一組**互相拉扯**的保證同時成立:個資真的消失了,但會計憑證
沒有跟著陪葬,而且抹除完的帳號進不來。少任何一條,這個功能就不能上線 ——
只砍個資不封帳號等於留了後門,只封帳號不砍個資等於什麼都沒做。
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.security import get_password_hash
from app.models.buyer_info import BuyerInfo
from app.models.event import Event, EventStatus
from app.models.order import Order, OrderStatus
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.erasure import erase_user, is_erased
from app.services.pii import encrypt_pii, lookup_hash

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def subject(db) -> User:
    """一個「完整」的使用者:有實名資料、有 session、有一筆訂單。"""
    user = User(username="被抹除的人", hashed_password=get_password_hash("secret123"))
    db.add(user)
    await db.flush()

    ciphertext, dek = encrypt_pii("A123456789")
    db.add(
        BuyerInfo(
            user_id=user.id,
            real_name="王小明",
            national_id_ciphertext=ciphertext,
            national_id_dek_encrypted=dek,
            national_id_lookup_hash=lookup_hash("A123456789"),
        )
    )

    now = datetime.now(timezone.utc)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash="h" * 64,
            expires_at=now + timedelta(days=1),
            absolute_expires_at=now + timedelta(days=30),
        )
    )

    event = Event(
        name="Concert", venue="Arena",
        starts_at=now + timedelta(days=30), ends_at=now + timedelta(days=30, hours=3),
        sale_starts_at=now - timedelta(days=1), sale_ends_at=now + timedelta(days=1),
        total_seats=100, price_cents=1500, status=EventStatus.PUBLISHED,
    )
    db.add(event)
    await db.flush()
    db.add(
        Order(
            user_id=user.id, event_id=event.id, quantity=2,
            total_price_cents=3000, status=OrderStatus.CONFIRMED,
            confirmed_at=now, idempotency_key=uuid4(),
        )
    )
    await db.commit()
    return user


# ─ 抹除本身

async def test_pii_row_is_destroyed(db, subject) -> None:
    """crypto-shred:整列消失,ciphertext 與被包住的 DEK 一起走。

    每列一把 DEK 是關鍵 —— 共用一把 KEK 加密所有列的話,刪一列不會讓任何東西
    變得不可解,而備份裡的那份 ciphertext 就還原得回來。
    """
    await erase_user(db, user=subject)
    await db.commit()
    assert await db.scalar(
        select(BuyerInfo).where(BuyerInfo.user_id == subject.id)
    ) is None


async def test_username_is_anonymised_and_account_disabled(db, subject) -> None:
    await erase_user(db, user=subject)
    await db.commit()
    assert subject.username == f"erased-user-{subject.id}"
    assert subject.is_active is False
    assert is_erased(subject)


async def test_sessions_are_revoked(db, subject) -> None:
    """留著 refresh token 的話,匿名化只是換了個名字 —— 那個 session 還能換到
    access token,而它的主人已經沒有名字可以查了。"""
    await erase_user(db, user=subject)
    await db.commit()
    assert (await db.scalars(
        select(RefreshToken).where(RefreshToken.user_id == subject.id)
    )).all() == []


async def test_the_order_survives_and_still_points_at_the_user(db, subject) -> None:
    """整個設計的取捨在這一條:訂單是會計憑證,不隨個資消失。

    而且 user_id 仍然指得到一個活著的列(只是那個人已經匿名),不是 NULL ——
    限購對帳的 GROUP BY 與 keyset 分頁的索引前綴都靠它。
    """
    await erase_user(db, user=subject)
    await db.commit()
    order = await db.scalar(select(Order).where(Order.user_id == subject.id))
    assert order is not None
    assert order.total_price_cents == 3000
    assert order.status is OrderStatus.CONFIRMED


async def test_erasure_is_idempotent(db, subject) -> None:
    """抹除請求可能被重送。第二次是 no-op,不是錯誤 —— 也不該把 username
    改成 erased-user-erased-user-N 這種東西。"""
    await erase_user(db, user=subject)
    await db.commit()
    first = subject.username
    await erase_user(db, user=subject)
    await db.commit()
    assert subject.username == first


async def test_erasing_a_user_without_buyer_info_works(db) -> None:
    """從沒填過實名的使用者一樣是合法的抹除對象,不該因為少一列就炸。"""
    user = User(username="沒填過實名", hashed_password=get_password_hash("secret123"))
    db.add(user)
    await db.commit()
    await erase_user(db, user=user)
    await db.commit()
    assert is_erased(user)


# ─ FK 政策:DB 自己擋得住抄捷徑

async def test_db_refuses_to_delete_a_user_with_orders(db, subject) -> None:
    """fk_orders_user_id 是 RESTRICT。這條測試存在的理由是:抹除的正解寫在
    erasure.py 的 docstring 裡,但**註解不會變紅** —— 哪天有人覺得
    `db.delete(user)` 比較直接,擋下他的必須是資料庫。"""
    await db.delete(subject)
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_deleting_a_user_without_orders_cascades_pii_and_sessions(db) -> None:
    """沒有訂單就沒有保存義務,這時真的刪掉是合法的 —— 而 CASCADE 保證
    個資與 session 不會變成指不到人的孤兒列留在庫裡。"""
    now = datetime.now(timezone.utc)
    user = User(username="沒買過票", hashed_password=get_password_hash("secret123"))
    db.add(user)
    await db.flush()
    ciphertext, dek = encrypt_pii("B234567890")
    db.add_all([
        BuyerInfo(
            user_id=user.id, real_name="李小華",
            national_id_ciphertext=ciphertext, national_id_dek_encrypted=dek,
            national_id_lookup_hash=lookup_hash("B234567890"),
        ),
        RefreshToken(
            user_id=user.id, token_hash="g" * 64,
            expires_at=now + timedelta(days=1),
            absolute_expires_at=now + timedelta(days=30),
        ),
    ])
    await db.commit()
    user_id = user.id

    await db.delete(user)
    await db.commit()

    assert await db.scalar(select(BuyerInfo).where(BuyerInfo.user_id == user_id)) is None
    assert (await db.scalars(
        select(RefreshToken).where(RefreshToken.user_id == user_id)
    )).all() == []


# ─ 端點

async def test_endpoint_requires_admin(client, db, subject) -> None:
    token = await client.post(
        "/v1/auth/token", data={"username": "被抹除的人", "password": "secret123"}
    )
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    resp = await client.delete(f"/v1/users/{subject.id}", headers=headers)
    assert resp.status_code == 403


async def test_endpoint_erases_and_the_account_can_no_longer_log_in(client, db, subject) -> None:
    db.add(User(
        username="boss", hashed_password=get_password_hash("secret123"), is_admin=True
    ))
    await db.commit()
    token = await client.post(
        "/v1/auth/token", data={"username": "boss", "password": "secret123"}
    )
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}

    resp = await client.delete(f"/v1/users/{subject.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["username"] == f"erased-user-{subject.id}"

    # 抹除前的帳密再也進不來 —— is_active=False 與被換掉的 hash 兩道都擋。
    after = await client.post(
        "/v1/auth/token", data={"username": "被抹除的人", "password": "secret123"}
    )
    assert after.status_code == 401
