from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status, Request, Query
from redis.asyncio import Redis as RedisClient
from stripe import StripeClient
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud.user import get_user_by_id
from app.db.session import AsyncSessionLocal, get_db
from app.models.user import User
from app.schemas.token import TokenPayload
from app.services.rate_limit import enforce

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/token")
# EventSource can't send an Authorization header, so the SSE stream also reads the
# token from a query param; auto_error=False lets get_stream_user fall back to it.
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/v1/auth/token", auto_error=False)


def client_ip(request: Request) -> str:
    """真實客戶端 IP —— 限流與稽核共用同一個定義。

    `X-Forwarded-For` 是**附加式**的:ALB 把它看到的 TCP 對端接在既有值後面,所以
    最右邊那個才是可信的,左邊的都可能是客戶端自己塞進來的。常見寫法取最左邊,那等於
    讓任何人自稱任意 IP —— 每個請求換一個假 IP,限流就完全不存在。

    `TRUSTED_PROXY_COUNT=0`(預設)直接用 socket 對端,不看 XFF。這是安全的預設:
    設太小只是限流變嚴,設太大而前面沒有那麼多層代理就是誰都能繞過。

    三個消費者(每 IP 限流、稽核事件的 actor_ip、選區端點的限流 key)共用這一份 ——
    上一版三處各自寫死(`get_remote_address` / `request.client.host` × 2),於是
    「要正確處理代理」這個原則只在其中一處被想起來過,而且那一處也寫錯了。
    """
    trusted = get_settings().TRUSTED_PROXY_COUNT
    if trusted > 0:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            if hops:
                # 從右邊數第 trusted 個;鏈比宣告的短就退回最左邊(仍在鏈內)。
                return hops[max(0, len(hops) - trusted)]
    return request.client.host if request.client else "unknown"


async def enforce_ip_rate_limit(
    request: Request, redis: RedisClient, *, bucket: str, rate: str
) -> None:
    """每 IP 限流。`bucket` 區分不同端點,免得它們共用同一個計數器。

    key 的組法(bucket + client_ip)只在這裡出現一次。以前每個端點自己拼字串,而
    那正是「同一個原則被寫錯三次」的來源 —— 這個專案在 Redis key 格式上已經栽過
    兩次了,限流的 key 是第三次。
    """
    await enforce(redis, f"{bucket}:{client_ip(request)}", rate=rate)

DbSession = Annotated[AsyncSession, Depends(get_db)]

async def get_redis(request: Request) -> RedisClient:
    """Return the app-wide Redis client.
    
    Lifespan stored it on app.state; here we just hand it back so endpoints
    can use `Annotated[Redis, Depends(get_redis)]` for clean DI.
    """
    return request.app.state.redis
Redis = Annotated[RedisClient, Depends(get_redis)]

async def get_stripe(request: Request) -> StripeClient:
    """Return the app-wide async Stripe client (created in lifespan)."""
    return request.app.state.stripe
Stripe = Annotated[StripeClient, Depends(get_stripe)]

async def _load_user_from_token(token: str | None, db: AsyncSession) -> User:
    """Validate a JWT access token and load its user (no active-status check).

    Shared by the header-based get_current_user and the header-or-query
    get_stream_user. Raises 401 on a missing/invalid token or unknown user.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exception
    settings = get_settings()
    try:
        payload = TokenPayload(**jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM],
        ))
    except (InvalidTokenError, ValidationError):
        raise credentials_exception

    # `sub` 一律是 user.id 的字串。以前 /auth/token 發 username 而 /auth/refresh 發
    # user_id,這裡卻只用 username 查 —— 於是**每一張刷新過的 token 都 401**,而且
    # 一旦有人註冊了純數字的帳號名,他就會接收所有 id 等於那個數字的人的請求。
    # 舊的(sub=username)access token 在這裡會落到 401,客戶端拿 refresh cookie
    # 換一張就好;access token 本來就是分鐘級的,部署時最多一個 TTL 的抖動。
    if not payload.sub.isdigit():
        raise credentials_exception
    user = await get_user_by_id(db, user_id=int(payload.sub))
    if user is None:
        raise credentials_exception
    return user


async def get_current_user(
        token: Annotated[str, Depends(oauth2_scheme)],
        db: DbSession,
) -> User:
    return await _load_user_from_token(token, db)


async def get_current_active_user(
        current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )
    return current_user

CurrentUser = Annotated[User, Depends(get_current_active_user)]


async def get_stream_user(
        header_token: Annotated[str | None, Depends(oauth2_scheme_optional)] = None,
        access_token: Annotated[str | None, Query()] = None,
) -> User:
    """Auth for the SSE/EventSource stream: JWT from the Authorization header
    (preferred, for API clients) or an ?access_token= query param (for browsers —
    native EventSource can't set headers). Header wins if both are present.

    **刻意不用 `DbSession`。** FastAPI 的 yield 依賴要活到回應結束才收掉,而 SSE 的
    回應要到生成器跑完(最長 300 秒)才算結束 —— 用共用依賴的話,每一條串流都會
    抓著一條 DB 連線曬 5 分鐘。DB_POOL_SIZE=5 + DB_MAX_OVERFLOW=10 → 每個 process
    只能有 15 條並發 SSE,第 16 個等候者會卡在連線池上,而等候室正是「幾萬人同時
    連著」的地方。所以這裡自己開一個短命 session,查完就還。

    `expunge` 讓 user 脫離 session:session 關掉之後仍能讀已載入的欄位(engine 設了
    `expire_on_commit=False`,而這裡也沒有 commit),但任何 lazy load 會直接報錯 ——
    那正是我們要的,不要有人不小心在串流中途碰到一個關聯欄位而重新借連線。

    SECURITY: a token in the query string lands in access logs, browser history,
    and Referer headers. Mitigated by HTTPS + the access token's short lifetime;
    the hardened alternative is a dedicated short-lived, stream-scoped token.
    """
    async with AsyncSessionLocal() as db:
        user = await _load_user_from_token(header_token or access_token, db)
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user"
            )
        db.expunge(user)
    return user

StreamUser = Annotated[User, Depends(get_stream_user)]


async def get_current_admin(current_user: CurrentUser) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return current_user

CurrentAdmin = Annotated[User, Depends(get_current_admin)]