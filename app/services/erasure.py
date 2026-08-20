"""個資抹除:匿名化 + crypto-shred。

**不刪 users 那一列**,這是整個設計的核心取捨。訂單是會計憑證(商業會計法要求
保存五年),而個資法的刪除請求不凌駕法定保存義務 —— 所以「抹除」必須是「讓那些
訂單再也連不回一個活人」,而不是「把訂單一起帶走」。

保留一個匿名化的 user 列(而不是把 orders.user_id 設成 NULL)換到三件東西:

  1. 會計憑證仍然是完整的一列,不是帶著 NULL 外鍵的殘骸
  2. 限購對帳的 `GROUP BY user_id`(inventory.py)照常運作
  3. `ix_orders_user_created` 的前綴還在,keyset 分頁不退化

真正被銷毀的是 buyer_info 那一列。因為 PII 走的是每列一把 DEK 的信封加密
(pii.py),刪掉那一列 = ciphertext 與被 KEK 包住的 DEK 同時消失 —— 就算 KEK
還在、就算有人拿到整個資料庫,也解不回姓名與身分證號。這叫 crypto-shred,而
這個 schema 早就把機制蓋好了,只是一直沒有人去按。
"""
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.buyer_info import BuyerInfo
from app.models.refresh_token import RefreshToken
from app.models.user import User

#: 匿名化之後的 username。用 id 而不是隨機字串:唯一性由主鍵免費保證,而且
#: 「這是一個被抹除的帳號」在營運上看得出來 —— 隨機亂碼會讓人以為是資料損壞。
ANONYMISED_USERNAME = "erased-user-{user_id}"

#: 一個不可能比對成功的 hash 佔位。Argon2 的驗證會直接判定格式不符而失敗,
#: 不會意外匹配到任何密碼。真正擋住登入的是 is_active=False(deps.py 兩處都檢查),
#: 這一欄只是不留下一個仍然可用的憑證。
_UNUSABLE_PASSWORD = "!erased"


async def erase_user(db: AsyncSession, *, user: User) -> None:
    """就地匿名化一個使用者,並銷毀他的 PII。呼叫端負責 commit。

    順序有意義:先砍 session、再砍 PII、最後才動 users 那一列。反過來的話,
    中途失敗會留下一個「已匿名但 refresh token 還能換 access token」的帳號 ——
    而那個帳號已經沒有名字可以查了。
    """
    await db.execute(
        delete(RefreshToken).where(RefreshToken.user_id == user.id)
    )
    # crypto-shred。用 Core delete 而不是先 select 再 db.delete:少一趟往返,
    # 而且沒有「查不到就不刪」的分支要處理 —— 本來就沒有 buyer_info 的使用者
    # (從沒填過實名)一樣是合法的抹除對象。
    await db.execute(
        delete(BuyerInfo).where(BuyerInfo.user_id == user.id)
    )
    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(
            username=ANONYMISED_USERNAME.format(user_id=user.id),
            hashed_password=_UNUSABLE_PASSWORD,
            is_active=False,
        )
        .execution_options(synchronize_session=False)
    )
    # identity map 裡那個 user 物件現在是舊的。呼叫端(端點)拿它組回應,
    # 不 refresh 的話會回傳抹除前的 username。
    await db.refresh(user)


def is_erased(user: User) -> bool:
    """這個帳號被抹除過了嗎?重複的抹除請求要是 no-op,不是錯誤。"""
    return user.username == ANONYMISED_USERNAME.format(user_id=user.id)
