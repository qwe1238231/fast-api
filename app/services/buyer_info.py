"""Buyer info service — orchestrates PII encryption + DB insert.

Service layer is the only place that handles plaintext PII (briefly).
Caller (route) gives plaintext, gets back encrypted result.
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.exceptions import BuyerInfoAlreadyExists, NationalIdAlreadyRegistered
from app.crud.buyer_info import create_buyer_info, get_buyer_info_by_user_id
from app.models.buyer_info import BuyerInfo
from app.services.pii import encrypt_pii, lookup_hash

#: INSERT 會撞的兩條約束,名字是 Postgres 寫在錯誤訊息裡的那個:PK 走預設命名
#: (<表名>_pkey),唯一索引走 SQLAlchemy 的 ix_<欄位> 慣例(migration cb306b9096a7)。
#: 任一邊改名這裡要跟著改 —— 漏改不會錯分,會退回 raise 變 500,吵但誠實。
_PK_CONSTRAINT = "buyer_info_pkey"
_LOOKUP_HASH_INDEX = "ix_buyer_info_national_id_lookup_hash"


async def register_buyer_info(
        db: AsyncSession,
        *,
        user_id: int,
        real_name: str,
        national_id: str,
) -> BuyerInfo:
    """Create buyer info for a user. Encrypts PII before storing.

    Raises BuyerInfoAlreadyExists if user already has one.
    Raises NationalIdAlreadyRegistered if national_id is taken by another user.

    預檢 SELECT 留著,但它不是正確性的來源:兩個請求同時通過預檢的那條縫,靠 PK 與
    唯一索引收尾。預檢的價值是讓常態路徑的答案**確定**:同一個 user 重填時一定得到
    BuyerInfoAlreadyExists,不必依賴 Postgres 先檢查哪一個索引(那是 OID 順序,
    migration 鏈跟 create_all 建出來的不保證一樣)。

    撞到約束後按名字分流,不把所有 IntegrityError 都當成撞身分證(同 zones.py 的
    ZoneNameTaken):PK 衝突是「你已經填過了」,身分證索引衝突是「這張證是別人的」,
    外鍵或 CHECK 違反則是呼叫端的 bug,要原樣往上丟,不能講成使用者的錯。
    """
    existing = await get_buyer_info_by_user_id(db, user_id)
    if existing is not None:
        raise BuyerInfoAlreadyExists(user_id=user_id)

    envelope = encrypt_pii(national_id)
    lookup = lookup_hash(national_id)

    try:
        info = await create_buyer_info(
            db,
            user_id=user_id,
            real_name=real_name,
            national_id_ciphertext=envelope.ciphertext,
            national_id_dek_encrypted=envelope.dek_encrypted,
            national_id_lookup_hash=lookup,
            kek_version=envelope.kek_version,
        )
    except IntegrityError as exc:
        await db.rollback()
        detail = str(exc.orig)
        if _LOOKUP_HASH_INDEX in detail:
            raise NationalIdAlreadyRegistered() from exc
        if _PK_CONSTRAINT in detail:
            raise BuyerInfoAlreadyExists(user_id=user_id) from exc
        raise

    return info
