from app.services.pii import encrypt_pii, decrypt_pii, lookup_hash


def test_encrypt_decrypt_roundtrip():
    plaintext = "A123456789"
    ciphertext, dek_encrypted = encrypt_pii(plaintext)
    assert decrypt_pii(ciphertext, dek_encrypted) == plaintext


def test_encryption_is_nondeterministic():
    # 同明文每次密文不同(隨機 DEK + nonce),但都解得回原文
    c1, d1 = encrypt_pii("A123456789")
    c2, d2 = encrypt_pii("A123456789")
    assert c1 != c2
    assert decrypt_pii(c1, d1) == decrypt_pii(c2, d2) == "A123456789"


def test_lookup_hash_is_deterministic():
    # 同明文 → 同 hash(才能用來查重);不同明文 → 不同 hash
    assert lookup_hash("A123456789") == lookup_hash("A123456789")
    assert lookup_hash("A123456789") != lookup_hash("B987654321")