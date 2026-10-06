"""信封加密與 KEK 版本:同明文不同密文、lookup hash 可查重,以及盤點 A3 —— 換鑰匙不停機。

輪替在測試裡的模擬方式:直接改 get_settings() 那個實例(monkeypatch),跟 rate_limiting
fixture 同一個手法 —— Settings 是 lru_cache 的,改環境變數沒用。pii_keyring 每次呼叫重新
解析,所以改完立刻生效。
"""
import base64
import json
import os

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.buyer_info import BuyerInfo
from app.models.user import User
from app.services.pii import (
    PiiKeyVersionUnknown, active_kek_version, decrypt_pii, encrypt_pii, lookup_hash, rewrap_dek,
)
from app.worker import rewrap_pii_keks


def _b64_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def test_encrypt_decrypt_roundtrip():
    env = encrypt_pii("A123456789")
    assert env.kek_version == active_kek_version()
    assert decrypt_pii(env.ciphertext, env.dek_encrypted, kek_version=env.kek_version) == "A123456789"


def test_encryption_is_nondeterministic():
    # 同明文每次密文不同(隨機 DEK + nonce),但都解得回原文
    a, b = encrypt_pii("A123456789"), encrypt_pii("A123456789")
    assert a.ciphertext != b.ciphertext
    assert (
        decrypt_pii(a.ciphertext, a.dek_encrypted, kek_version=a.kek_version)
        == decrypt_pii(b.ciphertext, b.dek_encrypted, kek_version=b.kek_version)
        == "A123456789"
    )


def test_lookup_hash_is_deterministic():
    # 同明文 → 同 hash(才能用來查重);不同明文 → 不同 hash
    assert lookup_hash("A123456789") == lookup_hash("A123456789")
    assert lookup_hash("A123456789") != lookup_hash("B987654321")


# ─ KEK 輪替

@pytest.fixture
def rotated(monkeypatch):
    """模擬輪替:先用現在的鑰匙包一個信封,然後現役換成下一版的新鑰匙、原本的退役。
    回傳那個用**舊**鑰匙包的信封。"""
    settings = get_settings()
    old_key, old_version = settings.PII_KEK_BASE64, settings.PII_KEK_VERSION
    before = encrypt_pii("A123456789")
    monkeypatch.setattr(settings, "PII_KEK_RETIRED", json.dumps({str(old_version): old_key}))
    monkeypatch.setattr(settings, "PII_KEK_VERSION", old_version + 1)
    monkeypatch.setattr(settings, "PII_KEK_BASE64", _b64_key())
    return before


def test_old_rows_still_decrypt_after_rotation_and_new_rows_use_the_new_key(rotated):
    """輪替的第一個前提:換了鑰匙,舊列照樣讀得到 —— 鑰匙按列上的版本挑,不是只有現役一把。"""
    assert decrypt_pii(rotated.ciphertext, rotated.dek_encrypted, kek_version=rotated.kek_version) == "A123456789"
    fresh = encrypt_pii("B987654321")
    assert fresh.kek_version == rotated.kek_version + 1
    assert decrypt_pii(fresh.ciphertext, fresh.dek_encrypted, kek_version=fresh.kek_version) == "B987654321"


def test_rewrap_moves_the_dek_to_the_active_kek_without_touching_the_ciphertext(rotated):
    new_dek, version = rewrap_dek(rotated.dek_encrypted, kek_version=rotated.kek_version)
    assert version == active_kek_version()
    assert new_dek != rotated.dek_encrypted
    # 密文沒變,用重包後的 DEK 照樣解得開 —— 輪替便宜的原因就在這裡
    assert decrypt_pii(rotated.ciphertext, new_dek, kek_version=version) == "A123456789"


def test_an_unknown_kek_version_is_loud_not_a_4xx():
    env = encrypt_pii("A123456789")
    with pytest.raises(PiiKeyVersionUnknown) as excinfo:
        decrypt_pii(env.ciphertext, env.dek_encrypted, kek_version=99)
    assert excinfo.value.version == 99


# ─ Settings 守門:壞的 keyring 要在啟動時炸,不是第一次讀舊列才發現

def test_settings_reject_the_active_version_in_retired():
    base = get_settings().model_dump()
    with pytest.raises(ValidationError, match="PII_KEK_RETIRED"):
        Settings(**{**base, "PII_KEK_RETIRED": json.dumps({str(base["PII_KEK_VERSION"]): _b64_key()})})


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "[]",                                   # 不是物件
        json.dumps({"0": _b64_key()}),          # 版本要 >= 1
        json.dumps({"3": "too-short"}),         # 不是 32 bytes 的 base64
        json.dumps({"3": 42}),                  # 不是字串
    ],
)
def test_settings_reject_malformed_retired_keys(bad):
    base = get_settings().model_dump()
    with pytest.raises(ValidationError):
        Settings(**{**base, "PII_KEK_RETIRED": bad})


# ─ worker 的背景那一半

async def _row(db, username: str, envelope, *, kek_version: int | None = None) -> int:
    user = User(username=username, hashed_password="x")
    db.add(user)
    await db.flush()
    db.add(BuyerInfo(
        user_id=user.id, real_name="王小明",
        national_id_ciphertext=envelope.ciphertext,
        national_id_dek_encrypted=envelope.dek_encrypted,
        national_id_lookup_hash=lookup_hash(f"{username}-id"),
        kek_version=envelope.kek_version if kek_version is None else kek_version,
    ))
    await db.commit()
    return user.id


@pytest.mark.asyncio
async def test_rewrap_cron_moves_old_rows_and_is_idempotent(db, rotated):
    uid = await _row(db, "rotated", rotated)

    assert await rewrap_pii_keks({}) == 1
    assert await rewrap_pii_keks({}) == 0            # 第二輪沒事做

    db.expire_all()
    row = await db.scalar(select(BuyerInfo).where(BuyerInfo.user_id == uid))
    assert row.kek_version == active_kek_version()
    assert row.national_id_ciphertext == rotated.ciphertext          # 密文一個 byte 都沒動
    assert row.national_id_dek_encrypted != rotated.dek_encrypted    # 動的只有包裝
    assert decrypt_pii(row.national_id_ciphertext, row.national_id_dek_encrypted, kek_version=row.kek_version) == "A123456789"


@pytest.mark.asyncio
async def test_rewrap_cron_skips_rows_on_a_version_it_cannot_open(db, rotated):
    """退役鑰匙被太早清掉的那種列:跳過、不改、不讓整輪炸掉;其他列照常處理。"""
    stuck = await _row(db, "stuck", rotated, kek_version=7)          # keyring 裡沒有第 7 版
    fine = await _row(db, "fine", rotated)

    assert await rewrap_pii_keks({}) == 1                            # 只有 fine 被換

    db.expire_all()
    stuck_row = await db.scalar(select(BuyerInfo).where(BuyerInfo.user_id == stuck))
    fine_row = await db.scalar(select(BuyerInfo).where(BuyerInfo.user_id == fine))
    assert stuck_row.kek_version == 7
    assert stuck_row.national_id_dek_encrypted == rotated.dek_encrypted
    assert fine_row.kek_version == active_kek_version()
