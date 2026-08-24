from datetime import datetime
from enum import Enum
from uuid import UUID
from sqlalchemy import DDL, Index, event, text
from sqlalchemy import BigInteger, DateTime, ForeignKey, ForeignKeyConstraint, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Index, text, CheckConstraint
from app.db.base import Base

#: orders 的 autovacuum 調校。**沒有這個,上面那個 INCLUDE 覆蓋索引是白加的。**
#:
#: Index Only Scan 需要 visibility map 標記頁面 all-visible,而只有 vacuum 會設那個
#: 位元。實測(200 萬列,對帳查詢):
#:     VACUUM 之後    VM 100%   heap fetches   0    22 blocks   0.28 ms
#:     +4 萬筆未清     VM  98%   heap fetches  81   103 blocks   0.48 ms
#:     +40 萬筆未清    VM  83%   heap fetches 801   827 blocks   1.90 ms
#:
#: 40 萬正好是**預設值**的觸發點(1000 + 0.2 × 200 萬)—— 不調的話穩態就是最後那一列,
#: 覆蓋索引的效果被吃掉 37 倍。
#:
#: scale_factor 一律設 0、只留固定 threshold:scale_factor 在大表上是陷阱,它讓觸發點
#: 跟著表一起長,2000 萬列時就算設 0.02 也回到 40 萬。固定值才能讓「落後多少」與表
#: 大小脫鉤。
#:
#: 調勤不會等比例變貴 —— vacuum 自己也用 VM 跳過乾淨的頁面,成本跟髒頁數成正比而不是
#: 表大小:實測 40 萬筆髒頁 51ms、4 萬筆 19ms、完全乾淨 18ms。
#:
#: 代價:訂單還不到兩萬筆的階段幾乎不會被 autovacuum 碰。那時候沒有東西值得清,
#: analyze 的門檻設得低一些讓查詢計畫仍然跟得上。
ORDERS_AUTOVACUUM = {
    "autovacuum_vacuum_insert_threshold": 20000,
    "autovacuum_vacuum_insert_scale_factor": 0,
    "autovacuum_vacuum_threshold": 20000,
    "autovacuum_vacuum_scale_factor": 0,
    "autovacuum_analyze_threshold": 10000,
    "autovacuum_analyze_scale_factor": 0,
}



class OrderStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"



class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        # INCLUDE 把對帳查詢變成 Index Only Scan。不是為了單次查詢快 20ms ——
        # 是因為 detect_inventory_drift **每 5 分鐘對每一個 published 場次各跑一次**
        # compute_expected_available,所以成本是「場次數 × 每場 heap block」。
        #
        # 實測(每場約 4000 筆持有中的訂單):
        #   訂單實體打散(普通場次、長售期)  4000 blocks/場 → 300 場一輪 ≈ 9.4 GB
        #   訂單實體集中(熱門場次爆量)        58 blocks/場 → 一輪 ≈ 136 MB
        #   打散 + INCLUDE                     23 blocks/場 → 一輪 ≈ 54 MB
        #
        # 打散那一列的問題不是慢,是每 5 分鐘把 shared_buffers 沖一遍 —— 而被排擠掉的
        # 正是下單路徑需要的頁面。熱門場次自己用不到這個索引,但普通場次會一直在
        # 背景製造那個負載,而長售期讓它們待在保留窗裡更久、被掃更多輪。
        #
        # 寫入代價幾乎是零:user_id 與 quantity 建立後不再改(CAS 只動 status 與
        # 時間戳),所以不會製造額外的索引更新;HOT 也沒有損失,因為 status 本來就
        # 在索引裡,狀態轉換早就打斷 HOT 了。每個 tuple 只是大 8 bytes。
        Index(
            "ix_orders_active", "event_id", "status",
            postgresql_include=["user_id", "quantity"],
            postgresql_where=text("status IN ('pending', 'paid', 'confirmed')"),
        ),
        # Keyset pagination for list_orders_for_user: equality on user_id, then
        # the (created_at, id) sort/cursor columns -> single index range scan.
        Index("ix_orders_user_created", "user_id", "created_at", "id"),
        # Expiry sweep (worker.expire_pending_orders) filters exactly these rows every
        # minute; a partial index on created_at keeps that a tiny range scan instead of
        # a seq scan of orders, and stays small (rows drop out as they leave PENDING).
        Index(
            "ix_orders_pending_sweep", "created_at",
            postgresql_where=text("status = 'pending' AND payment_provider_id IS NULL"),
        ),
        CheckConstraint(
            "status IN ('pending', 'paid', 'confirmed', 'expired', 'cancelled')",
            name="ck_orders_status",
        ),
        # Values must stay sane even when a row is INSERTed by the order-consumer
        # worker off a Redis Stream payload (which bypasses the Pydantic request layer).
        CheckConstraint("quantity > 0", name="ck_orders_quantity_pos"),
        CheckConstraint("total_price_cents >= 0", name="ck_orders_total_nonneg"),
        # 每個 milestone status 必須有對應的時間戳(forward correspondence)。
        CheckConstraint(
            "status <> 'paid' OR paid_at IS NOT NULL",
            name="ck_orders_paid_at",
        ),
        CheckConstraint(
            "status <> 'confirmed' OR confirmed_at IS NOT NULL",
            name="ck_orders_confirmed_at",
        ),
        CheckConstraint(
            "status <> 'expired' OR expired_at IS NOT NULL",
            name="ck_orders_expired_at",
        ),
        CheckConstraint(
            "status <> 'cancelled' OR cancelled_at IS NOT NULL",
            name="ck_orders_cancelled_at",
        ),
        # 上面四條是 forward correspondence(status → 時間戳)。反過來那一半原本沒有,
        # 於是一列可以同時帶著 expired_at 與 cancelled_at —— 而狀態機裡三個終態互斥,
        # 到得了其中一個就到不了另一個。這種列不會讓任何程式碼出錯,它只會讓對帳報表
        # 把同一筆算兩次,而那種錯誤沒有人會在當下發現。
        CheckConstraint(
            "expired_at IS NULL OR cancelled_at IS NULL",
            name="ck_orders_terminal_exclusive",
        ),
        # CONFIRMED 只能從 PAID 來(見 crud/order.py 的 _VALID_TRANSITIONS),所以
        # 「已確認但從沒付款」是狀態機到不了的狀態。
        CheckConstraint(
            "confirmed_at IS NULL OR paid_at IS NOT NULL",
            name="ck_orders_confirmed_needs_paid",
        ),
        CheckConstraint(
            "confirmed_at IS NULL OR paid_at <= confirmed_at",
            name="ck_orders_paid_before_confirmed",
        ),
        # **刻意沒有**加「expired/cancelled 就不可能有 paid_at」。今天的狀態機確實
        # 保證了(PAID 只能往 CONFIRMED),但那是一條會變的營運規則 —— 哪天要支援
        # 已付款訂單的退款/作廢,PAID → CANCELLED 就會變合法,而那時這條 CHECK 會
        # 變成擋路的東西。上面三條則是不管退款怎麼做都成立的。
        # (event_id, zone_id) 指向 event_zone_prices 的**主鍵**。取代原本的單欄
        # fk_orders_zone_id —— 那條只保證「zone 存在」,擋不住「這一區根本不屬於
        # 這場次的場館」。zone 的存在性由 event_zone_prices.zone_id 遞移保證,
        # 所以單欄那條是冗餘的,留著只是在最熱的寫入路徑上多一次查表。
        #
        # 附帶保證:有訂單的 zone 一定有票價。少了價格列的 zone 算進 total_seats
        # 卻永遠賣不掉(見 publish_event 的 ZonePricesIncomplete),現在連「訂單指
        # 到沒定價的區」這條路也被 DB 封死。
        #
        # NULL 語意靠 Postgres 的預設 MATCH SIMPLE:任一參照欄位是 NULL 就整條放行。
        # 所以無座位圖的場次(zone_id IS NULL)完全不受影響。**不要改成 MATCH FULL**
        # —— event_id 是 NOT NULL,那會讓 zone_id 變成實質必填,把非座位場次全擋死。
        ForeignKeyConstraint(
            ["event_id", "zone_id"],
            ["event_zone_prices.event_id", "event_zone_prices.zone_id"],
            name="fk_orders_event_zone_price",
        ),
    )
    # BIGINT。int4 的 21 億上限對訂單不是理論值:**失敗的 INSERT 一樣吃掉序列值**,
    # 而搶票系統的 idempotency 衝突與各種被擋下的下單都會走到那一步。真的滿了才改
    # 是一次全表重寫的停機,現在改是毫秒。
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # RESTRICT 不是預設值的同義詞,是一句宣告:**訂單不隨使用者消失**。訂單是會計
    # 憑證(商業會計法要求保存五年),個資法的刪除請求不凌駕法定保存義務,所以
    # 「抹除使用者」的正解是匿名化而不是連坐刪除 —— 見 app/services/erasure.py。
    # 這條 FK 的作用是讓任何人想抄捷徑時,資料庫會先擋下來。
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_orders_user_id"),
        nullable=False,
    )  # covered by ix_orders_user_created
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False)  # covered by ix_orders_active (event_id, status)
    # 買的是哪一區。分區票價下這是必要的來源資訊,但金額本身仍以
    # total_price_cents 的快照為準(所以之後改價動不到已成立的訂單,
    # webhook 的金額驗證也不必重算)。nullable:無座位圖的場次留空。
    # 外鍵是 __table_args__ 裡的複合 fk_orders_event_zone_price,不是單欄的。
    zone_id: Mapped[int | None] = mapped_column(nullable=True)
    quantity: Mapped[int] = mapped_column(nullable=False)
    total_price_cents: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(
            OrderStatus,
            native_enum=False,
            values_callable=lambda x :[e.value for e in x],
            ),
        nullable=False,
        default=OrderStatus.PENDING,
    )
    idempotency_key: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        unique=True,
        index=True,
    )
    payment_provider_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# SQLAlchemy 的 postgresql_with 只支援 Index,Table 沒有對應的 kwarg —— 所以 table
# storage parameter 只能靠 after_create 的 DDL。跟 seating.py 建 btree_gist、
# audit_log.py 建 DEFAULT 分區是同一個手法。
#
# 掛在 model 上而不是只寫進 migration,是為了讓兩條建表路徑得到同一份設定:migration
# 走 ALTER TABLE,測試走 metadata.create_all。少了這個,「model 說的」跟「線上跑的」
# 又多一個會無聲分岔的面 —— 而那正是 app/scripts/check_schema_drift.py 在守的東西。
event.listen(
    Order.__table__,
    "after_create",
    DDL(
        "ALTER TABLE orders SET ("
        + ", ".join(f"{k} = {v}" for k, v in ORDERS_AUTOVACUUM.items())
        + ")"
    ),
)