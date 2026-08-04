"""座位相關的對外表示。

一條貫穿的規則:**確認前絕不吐座號**。那是 pending hold 能被 compaction 滑動的
唯一前提 —— 使用者看過的東西不能偷偷改。所以選區階段只回「這一區有幾席、哪些
張數配得出來」,座號要等訂單 CONFIRMED 之後才出現。
"""
from pydantic import BaseModel


class ZoneAvailability(BaseModel):
    """選區畫面上的一列。"""

    zone_id: int
    name: str
    display_order: int
    """越小越靠舞台。前端照這個排序。"""

    price_cents: int
    available: int
    """這一區剩餘席數。0 = 該區售完(不代表整場售完)。"""

    available_quantities: list[int]
    """現在配得出來的張數。前端拿它直接 disable 掉選不了的選項。

    **這不是「最大連號長度」。** 只剩一段 5 連號時 4 張是配不出來的(會留下孤兒),
    回報 max_contiguous=5 然後拒絕 4 張正是客服災難的來源。所以回的是集合。
    """


class SeatedOrderDetail(BaseModel):
    """訂單的座位資訊。只在訂單已確認時才有內容。"""

    zone_name: str
    row_label: str
    block_index: int
    """同一排被走道切出的第幾段(0 起算)。

    少了它,「A 排 3、5、7 號」在一排被走道切成三段的場館裡**指不出實體位置** ——
    而門牌只在 block 內唯一(`uq_seats_block_label`),跨 block 撞號是允許的。
    目前的三種 labeller 都不會撞(連號或單雙分邊),但那是 labeller 的性質、
    不是約束保證的,所以票面必須帶著段號。
    """

    labels: list[str]
    """實際門牌。可能不連號(場館會跳過含 4 的號碼與 13,或單雙號分邊),
    但 pos 一定連續 —— 那才是「坐在一起」的判準。"""
