from functools import lru_cache
from pydantic_settings import BaseSettings,SettingsConfigDict
from pydantic import field_validator, model_validator
from datetime import timedelta
import base64


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REDIS_URL: str = "redis://localhost:6380/0"
    DEBUG:bool = False
    # SQL statement echo. Decoupled from DEBUG on purpose: load tests need the
    # bypass (which requires DEBUG) WITHOUT paying for a synchronous log line per
    # statement. Keep False unless you're actually debugging SQL.
    SQL_ECHO: bool = False
    # Load-test escape hatch: skip the waiting-room admission check on POST /orders so
    # the order/inventory path can be pressure-tested directly. Guarded below — only
    # honoured when DEBUG is on; the app refuses to start if this is set without DEBUG.
    LOADTEST_BYPASS_ADMISSION: bool = False
    ENABLE_MOCK_PAYMENT: bool = False        # /orders/{id}/pay 的模擬付款,DEBUG 專用
    REFRESH_TOKEN_SLIDING_DAYS: int = 14
    REFRESH_TOKEN_ABSOLUTE_EXPIRE_DAYS: int = 30
    REFRESH_TOKEN_REUSE_GRACE_SECONDS: int =10
    PII_KEK_BASE64: str
    PII_LOOKUP_KEY_BASE64:str
    STRIPE_SECRET_KEY: str
    STRIPE_WEBHOOK_SECRET: str = ""
    """Stripe webhook 的簽章密鑰。**非 DEBUG 下不得為空** —— 見 _guard_webhook_secret。"""
    LOGIN_RATE_LIMIT: str = "5/minute"
    REFRESH_RATE_LIMIT: str = "30/minute"
    REGISTER_RATE_LIMIT: str = "10/hour"
    """註冊的每 IP 上限。註冊是「未認證 + 寫 DB + 跑一次 Argon2」—— 三個特徵湊在
    一起就是最好用的放大攻擊面,而且黃牛本來就要大量帳號。放寬一點(10/小時而不是
    跟登入一樣 5/分鐘)是因為 NAT 後面會有整間公司/整棟宿舍共用一個 IP。"""

    RATE_LIMIT_ENABLED: bool = True
    """限流總開關。**只給測試關**(conftest 設成 False)。

    大多數測試會重複登入/註冊,不關的話它們會為了跟自己想測的東西無關的理由變紅。
    正式環境不要碰 —— 這裡沒有 fail-closed 守衛,因為關掉限流不會讓資料出錯,只會
    讓被濫用時比較痛;而加一個「必須 DEBUG」的守衛反而會擋住「正式環境臨時關掉限流
    來救火」這個合理需求。
    """

    LOGIN_FAILURE_LIMIT: int = 10
    LOGIN_FAILURE_WINDOW_SECONDS: int = 900
    """單一帳號在 LOGIN_FAILURE_WINDOW_SECONDS 內失敗幾次就暫時擋下。

    **為什麼需要它**:每 IP 限流擋不住有殭屍網路的人 —— 每個 IP 都在 5 次/分鐘以內,
    合起來仍然是每分鐘幾千次猜測。要擋住這個,計數必須掛在被攻擊的**帳號**上。

    **代價要講清楚**:這讓任何人都能故意打錯 10 次來把別人擋在門外 15 分鐘。這是
    帳號鎖定固有的取捨,業界的做法是「窗短、門檻寬、自動解鎖」而不是永久鎖定 ——
    所以這裡是 10 次 / 15 分鐘且自動過期,不是「鎖到管理員解鎖」。正常人 15 分鐘內
    打錯 10 次密碼是罕見的。
    """

    TRUSTED_PROXY_COUNT: int = 0
    """前面有幾層我們自己的反向代理(ALB=1、ALB+CloudFront=2、本機直連=0)。

    決定 `X-Forwarded-For` 要從右邊數第幾個才是真實客戶端。ALB 是**附加**在既有
    XFF 後面,所以最右邊那個是它親眼看到的 TCP 對端,左邊的都可能是客戶端自己塞的。
    取最左邊(常見寫法)等於讓任何人自稱任意 IP —— 限流直接失效。
    0 表示完全不信任 XFF,用 socket 對端。**寧可預設不信任**:設錯成 0 只是限流
    變嚴,設錯成 1 而前面其實沒有代理,就是誰都能繞過。
    """

    AUDIT_LOG_RETENTION_DAYS: int = 90

    MAX_TICKETS_PER_USER_PER_EVENT: int = 4
    """每人每場次的限購張數。**含 PENDING** —— 下單當下就佔額度,不然同時送 20 筆
    請求時全部都還沒 CONFIRMED,限購形同虛設。過期/取消會退回額度。

    這是每場次獨立的(鍵是 `event:{e}:purchased` 的 user_id 欄位),所以同一個人買
    不同藝人的票互不排擠。跟 MAX_TICKETS_PER_ORDER 是兩件事:那擋的是單一請求的
    大小,擋不住同一個人送很多筆。實際能買的是兩者取小 —— 見 `max_purchasable`。
    """

    MAX_TICKETS_PER_ORDER: int = 10
    """單筆訂單的張數上限。純粹是請求大小的護欄(擋掉 quantity=99999 這種),跟
    「這個人能買幾張」無關。"""

    # Async order-offload tuning
    ORDER_CONSUMER_BLOCK_MS: int = 2000        # how long the consumer loop blocks per read
    ORDER_RECLAIM_IDLE_MS: int = 60_000        # only reclaim entries idle at least this long
    ORDER_MAX_DELIVERIES: int = 5              # dead-letter after this many delivery attempts
    ORDER_BACKLOG_WARN: int = 1000             # log a warning when backlog exceeds this

    # DB connection pool (per-process). The total across ALL processes
    # (API workers + ARQ worker + order consumer) must fit Postgres max_connections:
    #   total ≈ num_processes * (DB_POOL_SIZE + DB_MAX_OVERFLOW)
    DB_POOL_SIZE: int = 5                       # persistent connections held open
    DB_MAX_OVERFLOW: int = 10                   # extra temporary connections at peak
    DB_POOL_PRE_PING: bool = True               # validate a connection before use (dead-conn defense)
    DB_POOL_RECYCLE: int = 1800                 # recycle connections older than this many seconds

    # Virtual waiting room (admission control). queue_opens/closes_at on an event
    # override these; otherwise they fall back to sale_starts_at minus the leads below.
    QUEUE_LEAD_TIME_SECONDS: int = 600          # registration opens this long before sale_starts_at
    ZONES_LIST_LIMIT_PER_MINUTE: int = 120   # 選區畫面的每 IP 每分鐘上限(無認證端點)
    QUEUE_ADMISSION_BUFFER_SECONDS: int = 30    # registration closes / admission begins this long before sale
    QUEUE_ADMISSION_RATE: int = 500             # users admitted per second (the gatekeeper throttle)
    QUEUE_ADMISSION_TOKEN_TTL_SECONDS: int = 120  # admitted buyers must complete within this window
    QUEUE_JOIN_LIMIT_PER_MINUTE: int = 30         # anti-hammer cap on queue-join per user per event
    # Circuit breaker: pause admission when the downstream order pipeline is unhealthy.
    ADMISSION_PAUSE_NEW_DEAD_LETTERS: int = 100
    """一次檢查(每分鐘)之內**新增**幾筆死信就暫停放行。

    刻意是增量不是總量。舊版比的是 `XLEN(orders:stream:dead)` —— 那個數字只增不減
    (沒有人會去清它),所以只要歷史上曾經壞過一次超過門檻,斷路器就**永遠**開著,
    整站永久停售,而且看起來完全像是「系統正在保護自己」。
    backlog 用總量是對的:那是佇列深度,消費者追上就會自己降下來。
    """
    ADMISSION_PAUSE_BACKLOG_THRESHOLD: int = 10000     # unpersisted backlog above this → pause

    model_config = SettingsConfigDict(env_file=".env",extra="ignore")

    @field_validator("PII_KEK_BASE64", "PII_LOOKUP_KEY_BASE64")
    @classmethod
    def _key_must_be_valid_base64(cls, v: str) -> str:
        try:
            decoded = base64.b64decode(v)
        except Exception:
            raise ValueError("key must be valid base64")
        if len(decoded) != 32:
            raise ValueError(
                f"decoded key must be 32 bytes, got {len(decoded)}"
                "Generate one: python -c \"import os, base64; print(base64.b64encode(os.urandom(32)).decode())\""
            )
        return v

    @model_validator(mode="after")
    def _guard_webhook_secret(self) -> "Settings":
        # 空字串是一個**已知的**密鑰,所以後果是完全顛倒的:真正的 Stripe webhook
        # 會驗簽失敗被拒,而任何人都能用空密鑰自算 HMAC 偽造 payment_intent.succeeded
        # 把訂單推成 CONFIRMED,或用別人的 order_id 送 payment_failed 作廢他人訂單
        # 並把座位吐回市場(_handle_payment_aborted 沒有 ownership 檢查)。
        #
        # 驗簽本身是對的(webhook.py 用 construct_event),缺的只是密鑰 —— 所以這裡
        # 用跟 LOADTEST_BYPASS_ADMISSION 同一套 fail-closed:寧可拒絕啟動。
        if not self.STRIPE_WEBHOOK_SECRET and not self.DEBUG:
            raise ValueError(
                "STRIPE_WEBHOOK_SECRET must be set when DEBUG=False; "
                "an empty secret is a KNOWN secret — anyone can forge Stripe webhooks"
            )
        return self

    @model_validator(mode="after")
    def _guard_mock_payment(self) -> "Settings":
        # ENABLE_MOCK_PAYMENT 讓 /orders/{id}/pay 把訂單一路推到 CONFIRMED 而不經過
        # Stripe。它在 v1 一直掛在正式 router 上、沒有任何閘門,任何登入使用者一個
        # curl 就能零元拿到票與座號。跟 LOADTEST_BYPASS_ADMISSION 同一個模式。
        if self.ENABLE_MOCK_PAYMENT and not self.DEBUG:
            raise ValueError(
                "ENABLE_MOCK_PAYMENT requires DEBUG=True; refusing to start"
            )
        return self

    @model_validator(mode="after")
    def _guard_loadtest_bypass(self) -> "Settings":
        # Fail-closed: the admission bypass must never be reachable in a non-DEBUG
        # (production-like) deployment. Refuse to boot rather than silently allow it.
        if self.LOADTEST_BYPASS_ADMISSION and not self.DEBUG:
            raise ValueError(
                "LOADTEST_BYPASS_ADMISSION requires DEBUG=True; refusing to start"
            )
        return self

    @property
    def access_token_lifetime(self) -> timedelta:
        return timedelta(minutes=self.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    @property
    def access_token_lifetime_seconds(self) -> int:
        return int(self.access_token_lifetime.total_seconds())

@lru_cache
def get_settings() -> Settings:
    return Settings()


def max_purchasable() -> int:
    """一個人在一筆請求裡實際買得到的張數上限 = min(單筆上限, 每人限購)。

    住在 config 而不是 seat_runs,是因為三個地方都要它 —— 請求 schema 的 `le=`、
    配位回報的「可行張數」、選區畫面的張數選單。上一版把 `MAX_TICKETS_PER_ORDER`
    放在 seat_runs 而 schema 自己寫死 `le=10`,靠一行「必須與 OrderCreate 一致」的
    註解綁著兩處 —— 註解不會在改壞的時候變紅。這個專案已經在 key 格式上踩過兩次
    同一個坑,所以上限也只留一個宣告點。
    """
    settings = get_settings()
    return min(settings.MAX_TICKETS_PER_ORDER, settings.MAX_TICKETS_PER_USER_PER_EVENT)