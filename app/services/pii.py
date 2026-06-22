"""PII envelope encryption service.

Wraps the Rust `ticket_secrets` primitives with envelope encryption pattern:
each row gets its own DEK, encrypted by the master KEK.
"""
import base64
import os 
import ticket_secrets
from app.core.config import get_settings


def _kek() -> bytes:
    """Master Key Encryption Key from settings."""
    return base64.b64decode(get_settings().PII_KEK_BASE64)

def _lookup_key() -> bytes:
    """HMAC key for searchable lookup hash."""
    return base64.b64decode(get_settings().PII_LOOKUP_KEY_BASE64)

def encrypt_pii(plaintext: str) ->tuple[bytes, bytes]:
    """Encrypt a PII string with envelope encryption.
    
    Returns (ciphertext, dek_encrypted) — store both in DB.
    """
    dek = os.urandom(32)
    ciphertext = ticket_secrets.aes_gcm_encrypt(dek, plaintext.encode("utf-8"))
    dek_encrypted = ticket_secrets.aes_gcm_encrypt(_kek(), dek)
    return ciphertext, dek_encrypted

def decrypt_pii(ciphertext: bytes, dek_encrypted: bytes) -> str:
    """Decrypt back to plaintext. Requires both columns from DB."""
    dek = ticket_secrets.aes_gcm_decrypt(_kek(), dek_encrypted)
    plaintext_bytes = ticket_secrets.aes_gcm_decrypt(dek, ciphertext)
    return plaintext_bytes.decode("utf-8")

def lookup_hash(plaintext: str) -> bytes:
    """Compute searchable HMAC for equality lookup.
    
    Use to query 'does this ID already exist' without decryption.
    """
    return ticket_secrets.hmac_sha256(_lookup_key(), plaintext.encode("utf-8"))