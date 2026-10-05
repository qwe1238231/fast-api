from datetime import datetime, timedelta,timezone
from typing import Any

import anyio.to_thread
import jwt
import ticket_secrets

from app.core.config import get_settings
import hashlib
import secrets 



def verify_password(plain_password: str ,hashed_password:str) -> bool:
    return ticket_secrets.verify_password(plain_password,hashed_password)

def get_password_hash(password:str) -> str:
    return ticket_secrets.hash_password(password)


async def hash_password_async(password: str) -> str:
    """Argon2 但不擋住 event loop。

    OWASP 參數下實測一次雜湊 ~220ms 純 CPU。在 async 端點裡直接呼叫同步版本,那
    220ms 之內**整個 worker process 的所有請求**都停住 —— 開賣瞬間別人的下單、
    SSE 心跳、健康檢查全都跟著卡。登入路徑早就用 threadpool 了,註冊卻沒有,而註冊
    是無認證端點:誰都能免費徵用你的 CPU。

    用 anyio 而不是 `fastapi.concurrency.run_in_threadpool`,是為了讓 core/ 維持
    零 FastAPI 依賴(crud 層也要呼叫這個)。Rust binding 會放掉 GIL,所以真的是
    平行跑而不只是讓出。
    """
    return await anyio.to_thread.run_sync(ticket_secrets.hash_password, password)


def create_access_token(
        subject: str,
        expires_delta: timedelta |None = None,
        extra_claims: dict[str, Any] | None = None,
)->str:
    setting= get_settings()
    now = datetime.now(timezone.utc)
    expire = now + (
        expires_delta 
        if expires_delta is not None
        else setting.access_token_lifetime
    )
    payload: dict[str, Any]={
        "sub": subject,
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    if extra_claims:
        reserved = {"sub", "iat", "exp", "type"}
        if reserved & extra_claims.keys():
            raise ValueError(
                f"extra_claims cannot override reserved keys: {reserved}"
                )
        payload.update(extra_claims)
    return jwt.encode(
        payload,
        setting.SECRET_KEY,
        algorithm=setting.ALGORITHM,
    )

def create_admission_token(*, user_id: int, event_id: int, ttl_seconds: int) -> str:
    """Short-lived, single-use pass proving the user was admitted to buy `event_id`.
    Signed with the app secret (HS256); the order endpoint verifies it before reserving.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "event_id": event_id,
        "typ": "admission",
        "jti": secrets.token_urlsafe(16),      # unique id → single-use enforcement in Redis
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


_DUMMY_PASSWORD_HASH = get_password_hash("dummy-password-for-timing-defense")

def constant_time_dummy_verify(password: str)   -> None:
    """
    Run a verify_password against a dummy hash to keep timing constant.
    Use when no real user is found, so request duration matches the
    "user found, wrong password" path — defeats username enumeration
    via timing analysis.
    """
    verify_password(password, _DUMMY_PASSWORD_HASH)

def generate_refresh_token() -> tuple[str, str]:
    """Return (plaintext, sha256_hex). Plaintext 給 client、hex 存 DB。"""
    token = secrets.token_urlsafe(32)
    return token, hash_refresh_token(token)

def hash_refresh_token(token: str) -> str:
    """SHA-256 hex of a refresh token. 用同一個函數確保 issue / verify 一致。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()