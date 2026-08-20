"""declare an explicit ON DELETE policy for every FK pointing at users

先前四條指向 users 的外鍵全是 NO ACTION,結果是「有訂單的使用者刪不掉」—— 而
專案裡根本沒有刪除端點,所以那不是一個被擋下的功能,是一個從未被決定的問題。

這次把決定寫進 schema。四條各有各的理由,不是同一個政策套四次:

    orders          RESTRICT   訂單是會計憑證(商業會計法五年),個資法的刪除請求
                               不凌駕法定保存義務。抹除的正解是匿名化,不是連坐刪除。
    buyer_info      CASCADE    這一列就是個資本身。刪掉它 = crypto-shred。
    refresh_tokens  CASCADE    session 沒有主人就沒有意義。
    audit_logs      SET NULL   稽核要比它記錄的人活得久,否則「刪帳號」就等於
                               「湮滅紀錄」。這一欄本來就 nullable,不需要讓步。

Revision ID: c8f4a2e6b193
Revises: b7e2c94a10f3
Create Date: 2026-08-20 12:05:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c8f4a2e6b193'
down_revision: Union[str, Sequence[str], None] = 'b7e2c94a10f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: (table, column, 舊的 PG 預設名, 新名, ON DELETE)
_FKS: tuple[tuple[str, str, str, str, str], ...] = (
    ("orders", "user_id", "orders_user_id_fkey", "fk_orders_user_id", "RESTRICT"),
    (
        "buyer_info", "user_id",
        "buyer_info_user_id_fkey", "fk_buyer_info_user_id", "CASCADE",
    ),
    (
        "refresh_tokens", "user_id",
        "refresh_tokens_user_id_fkey", "fk_refresh_tokens_user_id", "CASCADE",
    ),
    (
        "audit_logs", "actor_user_id",
        "audit_logs_actor_user_id_fkey", "fk_audit_logs_actor_user_id", "SET NULL",
    ),
)


def _swap(table: str, column: str, drop: str, create: str, on_delete: str) -> None:
    """換掉一條外鍵的 ON DELETE 政策。

    ON DELETE 不能 ALTER,只能 drop 再 add。兩步在同一個 migration 的交易裡,所以
    外面看不到「沒有外鍵」的中間狀態。

    NOT VALID + VALIDATE 的理由跟 fk_orders_event_zone_price 一樣:一般的
    ADD FOREIGN KEY 會全表掃並在整段期間擋掉寫入,而 orders / refresh_tokens 都
    不是可以停寫的表。既有列本來就滿足同一條參照(只有 ON DELETE 換了),VALIDATE
    這一趟必定通過。
    """
    op.drop_constraint(drop, table, type_="foreignkey")
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {create} "
        f"FOREIGN KEY ({column}) REFERENCES users (id) "
        f"ON DELETE {on_delete} NOT VALID"
    )
    op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {create}")


def upgrade() -> None:
    for table, column, old, new, on_delete in _FKS:
        _swap(table, column, old, new, on_delete)


def downgrade() -> None:
    for table, column, old, new, _ in reversed(_FKS):
        op.drop_constraint(new, table, type_="foreignkey")
        op.create_foreign_key(old, table, "users", [column], ["id"])
