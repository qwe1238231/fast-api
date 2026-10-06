"""PII envelope encryption service.

Wraps the Rust `ticket_secrets` primitives with the envelope pattern: each row gets its
own DEK (data-encryption key) that encrypts the plaintext; the DEK is itself wrapped by
the master KEK (key-encryption key) from settings.

**KEK 有版本號(盤點 A3)。** 每一列記下「我的 DEK 是用第幾版 KEK 包的」
(buyer_info.kek_version),解密按那個版本從 keyring 挑鑰匙。這讓換 KEK 變成線上操作:
新鑰匙設成現役、舊鑰匙放進 PII_KEK_RETIRED,新寫入用新版,舊列由 worker.rewrap_pii_keks
逐批「解開 DEK、用新 KEK 重包」—— 只動包裝那一層(幾十 bytes),明文的密文一個 byte 都
不碰,這正是信封加密存在的理由。沒有版本號的話,「哪把鑰匙解這一列」只能靠猜,換鑰匙
就只剩停機重包全部列一條路。

lookup hash 的 HMAC 鑰匙(PII_LOOKUP_KEY_BASE64)**不在**這套版本機制裡:換它要重算每一列
的 hash,而重算需要明文,那是另一種規模的工作,今天不做。
"""
import base64
import os
from dataclasses import dataclass

import ticket_secrets

from app.core.config import get_settings


@dataclass(frozen=True)
class EncryptedPII:
    """一列個資的信封:密文、包好的 DEK、包它的 KEK 版本。三個欄位一起存、一起讀。"""

    ciphertext: bytes
    dek_encrypted: bytes
    kek_version: int


class PiiKeyVersionUnknown(RuntimeError):
    """列上記的 KEK 版本不在 keyring 裡 —— 通常是 PII_KEK_RETIRED 被太早清掉。

    不是 DomainError:這不是使用者的錯,是營運設定壞了,要以 500 加 alert 浮出來,
    不能被翻成 4xx 讓人以為是輸入問題。
    """

    def __init__(self, version: int) -> None:
        self.version = version
        super().__init__(f"no PII KEK for version {version}; check PII_KEK_RETIRED")


def active_kek_version() -> int:
    return get_settings().PII_KEK_VERSION


def _kek(version: int) -> bytes:
    try:
        return get_settings().pii_keyring[version]
    except KeyError:
        raise PiiKeyVersionUnknown(version) from None


def _lookup_key() -> bytes:
    """HMAC key for searchable lookup hash(格式在 Settings 的 validator 驗過)。"""
    return base64.b64decode(get_settings().PII_LOOKUP_KEY_BASE64)


def encrypt_pii(plaintext: str) -> EncryptedPII:
    """Encrypt a PII string with envelope encryption, wrapping the DEK with the active KEK."""
    version = active_kek_version()
    dek = os.urandom(32)
    ciphertext = ticket_secrets.aes_gcm_encrypt(dek, plaintext.encode("utf-8"))
    dek_encrypted = ticket_secrets.aes_gcm_encrypt(_kek(version), dek)
    return EncryptedPII(ciphertext=ciphertext, dek_encrypted=dek_encrypted, kek_version=version)


def decrypt_pii(ciphertext: bytes, dek_encrypted: bytes, *, kek_version: int) -> str:
    """Decrypt back to plaintext. `kek_version` is the row's own — which key wrapped its DEK."""
    dek = ticket_secrets.aes_gcm_decrypt(_kek(kek_version), dek_encrypted)
    return ticket_secrets.aes_gcm_decrypt(dek, ciphertext).decode("utf-8")


def rewrap_dek(dek_encrypted: bytes, *, kek_version: int) -> tuple[bytes, int]:
    """把一個用第 `kek_version` 版 KEK 包的 DEK,改用現役 KEK 重包。回傳 (新的 dek_encrypted, 現役版本)。

    明文密文完全不經手:解開的只是 32 bytes 的 DEK,馬上又包回去。這是輪替便宜的原因。
    """
    active = active_kek_version()
    dek = ticket_secrets.aes_gcm_decrypt(_kek(kek_version), dek_encrypted)
    return ticket_secrets.aes_gcm_encrypt(_kek(active), dek), active


def lookup_hash(plaintext: str) -> bytes:
    """Compute searchable HMAC for equality lookup.

    Use to query 'does this ID already exist' without decryption.
    """
    return ticket_secrets.hmac_sha256(_lookup_key(), plaintext.encode("utf-8"))
