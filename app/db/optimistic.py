"""樂觀鎖:把「有人搶先改過」翻譯成領域例外。

版本欄位本身由 SQLAlchemy 的 `version_id_col` 維護(見 app/models/event.py),它在
flush 時自動把 `WHERE version = :loaded_version` 加進 UPDATE。但那只守得住**這個
transaction 內**從讀出到寫回的那幾毫秒 —— 而真正危險的間隔是使用者盯著編輯頁的那
幾分鐘,它橫跨兩個 HTTP request,ORM 完全看不到。

所以一道完整的樂觀鎖需要兩道關卡,少了任何一道都會退化成「裝了等於沒裝」:

  1. `require_version()` —— 客戶端帶回來的版本 vs 我們現在讀到的版本。
     擋掉「表單開太久,底下的資料已經換人改過」。**這道才是重點**:少了它,
     每次請求比對的都是自己幾毫秒前才讀出來的值,永遠吻合、永遠放行。
  2. `stale_data_as_conflict()` —— flush 時 rowcount=0 的那道。
     擋掉「兩個請求前後腳進來」,關掉關卡 1 到 commit 之間的殘餘窗口。

兩道都翻成同一個 ConcurrentModification:對使用者而言是同一件事(重新載入再試),
呼叫端不必分辨是哪一道擋下的。

刻意**不**對 StaleDataError 註冊全域 handler:同一個例外也用在 ORM 刪除/批次更新
對不上列數的情況,那些是程式 bug,一律回 409 會把 bug 偽裝成「重試就好」。
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.core.exceptions import ConcurrentModification


class Versioned(Protocol):
    """任何掛了 `version_id_col` 的 model。"""
    version: int


def require_version(
        obj: Versioned,
        *,
        expected: int,
        resource: str,
        resource_id: int,
) -> None:
    """關卡 1:客戶端帶回的版本必須等於目前這一列的版本。

    在任何欄位被改動**之前**呼叫 —— 這樣衝突時 session 還是乾淨的,不需要 rollback。
    """
    if obj.version != expected:
        raise ConcurrentModification(
            resource=resource,
            resource_id=resource_id,
            expected_version=expected,
            current_version=obj.version,
        )


@asynccontextmanager
async def stale_data_as_conflict(
        db: AsyncSession,
        *,
        resource: str,
        resource_id: int,
        expected_version: int | None = None,
) -> AsyncIterator[None]:
    """關卡 2:包住 flush/commit,把 StaleDataError 翻成 ConcurrentModification。

    一定要 rollback:flush 失敗後 session 會停在 pending-rollback,之後任何一次使用
    都會炸在一個跟真正原因無關的例外上。get_db 的 `async with` 最終也會關掉 session,
    但那太晚了 —— 例外處理器或同一個請求後續的稽核寫入會先撞上。
    """
    try:
        yield
    except StaleDataError as exc:
        await db.rollback()
        raise ConcurrentModification(
            resource=resource,
            resource_id=resource_id,
            expected_version=expected_version,
        ) from exc
