"""座位圖與座位持有的資料模型。

分層原則(跟 events / orders 一樣的那條):

    venues → zones → seat_blocks → seats      場館幾何,venue-scoped,幾乎不變
    event_zone_prices                          票價,event-scoped,每場不同
    seat_holds                                 佔用,event-scoped,高頻變動

同一個場館辦 50 場,座位圖只有一份、佔用有 50 份。把幾何跟佔用混在一張表是
這類系統最常見的第一個錯誤。
"""
from datetime import datetime

from sqlalchemy import (
    DDL,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base

# seat_holds 的 EXCLUDE 用 gist 同時比對「整數相等」與「範圍重疊」,前者需要
# btree_gist。migration 會建這個 extension,但測試是走 metadata.create_all
# 建表的,所以掛一個 before_create 讓兩條路徑都拿得到。
event.listen(
    Base.metadata,
    "before_create",
    DDL("CREATE EXTENSION IF NOT EXISTS btree_gist"),
)


class Venue(Base):
    """場館。座位圖的根。"""

    __tablename__ = "venues"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Zone(Base):
    """看台區 / 價格級距。三個角色同時是它:

    1. 使用者「選區域」的選項
    2. 票價的單位(見 EventZonePrice)
    3. Redis free-run hash 的分片鍵 —— 所以它必須有穩定的整數 id

    因為票價已經編碼了跨 zone 的品質差異(越前面越貴),配位演算法**只能在單一
    zone 內比較座位品質**;拿整場館的空段餵進 allocate() 會讓便宜票配到貴區。
    """

    __tablename__ = "zones"
    __table_args__ = (
        UniqueConstraint("venue_id", "name", name="uq_zones_venue_name"),
        Index("ix_zones_venue_order", "venue_id", "display_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_order: Mapped[int] = mapped_column(nullable=False, default=0)
    """越小越靠舞台。只管排序與顯示;實際票價在 EventZonePrice。"""


class EventZonePrice(Base):
    """某場次某一區的票價。

    跟 Zone 分開是因為生命週期不同:zone 是場館幾何(幾乎不變),票價是場次屬性
    (每場不同)。訂單成立時把單價乘上張數寫進 `orders.total_price_cents` 當快照
    —— 之後改價不會動到已成立的訂單,webhook 的金額驗證也因此不需要重算。
    """

    __tablename__ = "event_zone_prices"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="ck_event_zone_prices_nonneg"),
    )

    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), primary_key=True)
    zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id"), primary_key=True)
    price_cents: Mapped[int] = mapped_column(nullable=False)


class SeatBlock(Base):
    """走道之間不可跨越的一段連續座位 —— **配位的原子單位,不是「排」**。

    一排被走道切成兩段時,兩段的座位不算連續,所以必須是兩個 block。把「排」
    當單位會配出跨走道的「連號」。

    quality_base / quality_edge 餵給 `BlockGeometry.calibrated()`:最中間的座位
    得 base、最邊緣得 edge,中間線性內插。不規則因素(柱子遮蔽、樓層、視角)一律
    吸收進這兩個數字 —— 要更細的粒度就切更多 block,不要在 block 內部破壞單峰,
    否則配位演算法用 clamp 取代枚舉的那個 O(1) 優化會靜默取到最差解。
    """

    __tablename__ = "seat_blocks"
    __table_args__ = (
        UniqueConstraint("zone_id", "row_label", "block_index", name="uq_seat_blocks_pos"),
        CheckConstraint("capacity > 0", name="ck_seat_blocks_capacity_pos"),
        CheckConstraint(
            "quality_edge >= 0 AND quality_edge <= quality_base AND quality_base <= 1",
            name="ck_seat_blocks_quality_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 不加 index:uq_seat_blocks_pos (zone_id, row_label, block_index) 的索引已經
    # 以 zone_id 開頭,完全覆蓋「按 zone 查 block」。這個 repo 有兩個 commit 專門在
    # 刪這種冗餘(drop redundant ix_orders_event_id / ix_orders_user_id)。
    zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id"), nullable=False)
    row_label: Mapped[str] = mapped_column(String(8), nullable=False)
    block_index: Mapped[int] = mapped_column(nullable=False)
    """同一排被走道切出的第幾段,0 起算。"""

    capacity: Mapped[int] = mapped_column(nullable=False)
    quality_base: Mapped[float] = mapped_column(nullable=False, default=1.0)
    quality_edge: Mapped[float] = mapped_column(nullable=False, default=0.5)


class Seat(Base):
    """一個實體座位。存在的理由只有一個:`pos` 與 `label` 必須分開。

        實際門牌  1  3  5  7  9      ← 單號側,可能跳過 4 與 13
        pos       0  1  2  3  4      ← 稠密索引,連續性只看這個

    用門牌判斷連續性,遇到「跳過 4 號」就會把 3 號與 5 號判成不相鄰。
    """

    __tablename__ = "seats"
    __table_args__ = (
        UniqueConstraint("block_id", "pos", name="uq_seats_block_pos"),
        UniqueConstraint("block_id", "label", name="uq_seats_block_label"),
        CheckConstraint("pos >= 0", name="ck_seats_pos_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("seat_blocks.id"), nullable=False)
    pos: Mapped[int] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(String(16), nullable=False)


class SeatHold(Base):
    """一筆訂單持有的連續座位區間 [start_pos, start_pos + length)。

    涵蓋**整個生命週期**:下單時建立(座號未對外公開)、付款確認時 confirmed_at
    落下(從此凍結不可移動)、釋放時整列刪除。一張表一條 EXCLUDE 就保護了
    pending 與 confirmed 兩種狀態,比「pending 一張表、confirmed 另一張表」的
    兩道半約束強。

    確認前不得對外揭露座號 —— 那是 pending hold 可以被 compaction 滑動的唯一
    前提。使用者看過的東西不能偷偷改。
    """

    __tablename__ = "seat_holds"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_seat_holds_order"),
        CheckConstraint("length > 0", name="ck_seat_holds_length_pos"),
        CheckConstraint("start_pos >= 0", name="ck_seat_holds_start_nonneg"),
        # 區間不得超出 block 的容量。這是**跨表**條件(capacity 在 seat_blocks),
        # 所以 CHECK 表達不了 —— 但用「生成欄位 + 複合外鍵」可以純宣告地表達:
        # 區間最後一個 pos 必須真的存在於這個 block 的 seats 裡。
        #
        # EXCLUDE 擋的是「兩個人同一張椅子」,這條擋的是另一種損壞:「賣出一張
        # 根本沒有那個位子的票」。兩者都到不了(配位與釋放的 Lua 都做了上界檢查),
        # 但那全是應用層的紀律,而這個 schema 的哲學是 DB 當最後一道網。
        #
        # 前提:pos 在 block 內是稠密的(0..capacity-1 無洞),那由 seeder 保證。
        # 所以「最後一個存在」⇒「整段都存在」。
        ForeignKeyConstraint(
            ["block_id", "last_pos"],
            ["seats.block_id", "seats.pos"],
            name="fk_seat_holds_last_seat",
        ),
        # 同場次同 block 內任兩筆 hold 的區間不得重疊 —— 由 Postgres 保證,
        # 不是由應用層保證。就算配位 Lua 有 bug、就算 stream 重放、就算之後
        # 有人改壞演算法,「兩個人拿到同一張椅子」在這裡物理上不可能發生。
        # 庫存超賣是退錢;現場兩人同座是衝突,所以座位比數量更需要這道網。
        # DEFERRABLE 是給 compaction 用的:整批滑動 hold 時中間狀態會短暫重疊,
        # 在那個 txn 裡 SET CONSTRAINTS ALL DEFERRED 就能做任意排列。
        ExcludeConstraint(
            ("event_id", "="),
            ("block_id", "="),
            (text("int4range(start_pos, start_pos + length)"), "&&"),
            name="ex_seat_holds_no_overlap",
            using="gist",
            deferrable=True,
            initially="IMMEDIATE",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False)
    block_id: Mapped[int] = mapped_column(ForeignKey("seat_blocks.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    start_pos: Mapped[int] = mapped_column(nullable=False)
    length: Mapped[int] = mapped_column(nullable=False)
    last_pos: Mapped[int] = mapped_column(
        Computed("start_pos + length - 1", persisted=True), nullable=False
    )
    """區間的最後一個 pos。生成欄位 —— 存在的唯一理由是讓上面那條複合外鍵
    有東西可以指。不要手動寫入。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """非 NULL = 已付款確認,座號已對外公開,從此不可移動。"""
