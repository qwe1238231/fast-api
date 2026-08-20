"""每個 mapped_column 的 Python 標註必須跟它的 SQL 型別對得上。

這條規則沒有守門的話,錯誤只會以「type checker 對某個欄位靜默失效」的形式存在 ——
不會有任何測試變紅,因為 runtime 根本不看標註。實際抓到過兩個:

    audit_logs.actor_ip                Mapped[int | None]  掛在 String(45) 上
    buyer_info.national_id_lookup_hash Mapped[str]         掛在 LargeBinary 上

第二個尤其麻煩:它是查詢鍵,標註說 str 會讓呼叫端以為可以拿字串去比對 bytea。

精神跟 CI 那條 `alembic check` 一樣 —— 那條比對「model 與 migration」,這條比對
「標註與欄位」。兩者都是靠人眼就會漏掉的一致性。
"""
import enum
import typing

import pytest

import app.models  # noqa: F401  — import 一次把所有 mapper 註冊進 registry
from app.db.base import Base


def _declared(annotation: object) -> tuple[object, bool]:
    """`Mapped[X | None]` → (X, True)。回傳 (基礎型別, 標註是否允許 None)。"""
    args = typing.get_args(annotation)
    inner = args[0] if args else annotation
    members = typing.get_args(inner)
    optional = type(None) in members
    if optional:
        rest = [m for m in members if m is not type(None)]
        inner = rest[0] if len(rest) == 1 else inner
    # dict[str, Any] → dict,list[int] → list:泛型的 origin 才拿得去跟
    # column.type.python_type(永遠是裸型別)比較。
    return typing.get_origin(inner) or inner, optional


def _mapped_columns():
    for mapper in Base.registry.mappers:
        hints = typing.get_type_hints(mapper.class_)
        for prop in mapper.column_attrs:
            annotation = hints.get(prop.key)
            if annotation is None:
                continue          # 沒標註的欄位不在這條規則的管轄範圍
            yield mapper.class_, prop.key, prop.columns[0], annotation


# 兩條規則都一次掃完全部欄位再一起斷言,不做 parametrize:這個檔案完全不碰 DB,
# 但 conftest 的 autouse fixture(清表、清 Redis)是**每個 test case** 跑一次的。
# 攤成 200 個 case 會讓一條純 Python 的檢查花掉 8 秒 —— 而這個套件的歷史教訓
# 就是別讓與斷言無關的固定成本乘以 case 數(見 conftest 的 Argon2 替身)。

def test_annotations_match_the_column_types() -> None:
    offenders = []
    for cls, _key, column, annotation in _mapped_columns():
        declared, _ = _declared(annotation)
        try:
            actual = column.type.python_type
        except NotImplementedError:
            continue              # 自訂型別沒宣告 python_type,無從比對
        if isinstance(declared, type) and issubclass(declared, enum.Enum):
            continue              # SAEnum 的 python_type 就是 enum 本身,已經一致
        if isinstance(declared, type) and issubclass(declared, actual):
            continue
        offenders.append(
            f"{cls.__tablename__}.{column.name}:標註 {declared},"
            f"但 {column.type} 給的是 {actual.__name__}"
        )
    assert not offenders, "標註與欄位型別對不上:\n    " + "\n    ".join(offenders)


def test_optional_annotations_match_nullability() -> None:
    """`Mapped[X | None]` 與 `nullable=` 必須同進退。

    兩個方向都會出事:標註寫 X 但欄位可空,呼叫端不會處理 None;標註寫
    `X | None` 但欄位 NOT NULL,呼叫端會寫出永遠走不到的分支。
    """
    offenders = []
    for cls, _key, column, annotation in _mapped_columns():
        # 主鍵在 SQLAlchemy 裡一律 nullable=False,標註不寫 Optional 是對的。
        if column.primary_key:
            continue
        _, optional = _declared(annotation)
        if optional != column.nullable:
            offenders.append(
                f"{cls.__tablename__}.{column.name}:標註 Optional={optional},"
                f"欄位 nullable={column.nullable}"
            )
    assert not offenders, "Optional 與 nullable 對不上:\n    " + "\n    ".join(offenders)
