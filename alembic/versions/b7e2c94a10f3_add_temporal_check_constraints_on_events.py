"""add temporal ordering CHECK constraints on events

events 已經有 total_seats > 0 與 price_cents >= 0 的 CHECK,理由是「管理員 typo
會毒害庫存邏輯」。四個時間欄位的先後關係是同一類問題卻沒防:sale_ends_at 早於
sale_starts_at 的場次不會噴任何錯,只是永遠賣不出一張票 —— 而這種故障沒有告警,
只有「為什麼沒人買」。

只加真正是不變量的三條。刻意**沒有**加 sale_ends_at <= starts_at:開演後還能不能
賣(現場票、加場)是營運政策不是資料完整性,寫進 CHECK 等於用 migration 綁死business rule。

Revision ID: b7e2c94a10f3
Revises: a3d5f81c2b64
Create Date: 2026-08-20 11:15:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7e2c94a10f3'
down_revision: Union[str, Sequence[str], None] = 'a3d5f81c2b64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKWARD_INCOMPATIBLE = """\
新增 CHECK 之後,滾動部署期間還在跑的舊 task 沒有 EventCreate._check_window,
所以它送出的違規列會被 Postgres 擋下、變成 500 而不是 422。

判定是安全的,理由是「舊程式碼會寫出違規列」這件事在這裡不成立:app/ 裡只有
create_event 與 apply_event_update 會寫這四個欄位,兩者都是整批寫入,沒有任何
路徑會單獨挪動 ends_at 而讓它早於 starts_at。唯一能造出違規列的是管理員打錯
日期 —— 那正是這條 CHECK 要擋的東西,在部署窗內被擋下來也仍然是對的結果,
只是錯誤訊息比較難看。窗口是幾十秒,而且只影響「本來就會建出壞資料」的請求。

反過來拆成 expand/contract 兩次部署在這裡沒有意義:CHECK 沒有 expand 的那一半
可拆(先加一個永遠為真的約束再收緊?那第二次部署一樣要面對同一個窗口)。\
"""


_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("ck_events_show_window", "starts_at < ends_at"),
    ("ck_events_sale_window", "sale_starts_at < sale_ends_at"),
    # 兩欄都 nullable,只有同時給定時才有先後可言。
    (
        "ck_events_queue_window",
        "queue_opens_at IS NULL OR queue_closes_at IS NULL "
        "OR queue_opens_at < queue_closes_at",
    ),
)


def upgrade() -> None:
    # 這裡用一般的 ADD CHECK(不像 orders 那條走 NOT VALID/VALIDATE):ADD CHECK
    # 要拿 ACCESS EXCLUSIVE 並全表掃,在 orders 上不可接受,但 events 是幾百列的
    # 小表、又整份被快取著,掃描時間可以忽略。
    for name, condition in _CONSTRAINTS:
        op.create_check_constraint(name, "events", condition)


def downgrade() -> None:
    for name, _ in reversed(_CONSTRAINTS):
        op.drop_constraint(name, "events", type_="check")
