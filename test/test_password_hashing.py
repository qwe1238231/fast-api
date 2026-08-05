"""真正的 Argon2 —— 唯一不用測試替身的地方。

conftest 的 `_fast_password_hashing` 把雜湊換成 SHA-256 的替身(Argon2 佔整套測試
69% 的時間)。那個替身讓覆蓋率出現一個洞:沒有任何測試證明真的 KDF 還能用。
這個檔案就是補那個洞,所以每一條都標 `real_hashing`。
"""
import pytest

from app.core.security import get_password_hash, verify_password


@pytest.mark.real_hashing
def test_a_real_hash_round_trips() -> None:
    hashed = get_password_hash("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)


@pytest.mark.real_hashing
def test_a_real_hash_rejects_the_wrong_password() -> None:
    hashed = get_password_hash("correct horse battery staple")
    assert not verify_password("Correct horse battery staple", hashed)
    assert not verify_password("", hashed)


@pytest.mark.real_hashing
def test_real_hashes_are_salted() -> None:
    """同一個密碼兩次雜湊必須不同 —— 否則相同密碼的使用者會有相同的 hash。"""
    a = get_password_hash("same-password")
    b = get_password_hash("same-password")
    assert a != b
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)


@pytest.mark.real_hashing
def test_a_real_hash_is_argon2_and_fits_the_column() -> None:
    """欄位是 String(255)。Argon2 的編碼格式約 95 字元,但參數若被調大會變長 ——
    這條在它撐爆欄位之前就會紅。"""
    hashed = get_password_hash("x")
    assert hashed.startswith("$argon2"), hashed[:16]
    assert len(hashed) <= 255


def test_the_fake_is_active_by_default() -> None:
    """沒有標記時用的是替身。這條同時證明替身真的被裝上了 —— 否則「整套變快」
    可能只是別的原因,而我們會以為覆蓋率沒問題。"""
    assert get_password_hash("anything").startswith("faketest$")
    assert verify_password("anything", get_password_hash("anything"))
