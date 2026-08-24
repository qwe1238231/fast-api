from functools import lru_cache
from pydantic_settings import BaseSettings,SettingsConfigDict
from pydantic import Field, field_validator, model_validator
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
    STRIPE_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0)
    """打 Stripe 的逾時。**預設值(80 秒)必須改掉。**

    它比 `REQUEST_TIMEOUT_SECONDS` 長太多,結果是外層先放棄:客戶端拿到 504、我們
    這邊沒有任何一行說明是 Stripe 慢了,而 webhook 那條路更糟 —— 去重標記已經提交,
    Stripe 重送會被當成處理過,於是那筆退款靜靜地不見了。

    設得比請求逾時短,失敗的順序就反過來:Stripe 呼叫先自己失敗,我們記下
    `refund_failed`(needs_human),值班的人知道要手動退。validator 釘住這個關係。
    """

    STRIPE_EVENT_RETENTION_DAYS: int = Field(default=90, ge=7)
    """`stripe_events` 去重表的保留天數。

    Stripe 最多重送三天,所以去重本身只需要幾天;留 90 天是為了對帳(那張表是**我們
    這一側**看到的事件紀錄,比 Stripe 儀表板好用)。下限 7 天:短於重送窗的話,去重
    表會在 Stripe 還可能重送的期間就把紀錄刪掉 —— 那等於沒有去重。
    """

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

    CORS_ALLOW_ORIGINS: str = ""
    """允許的瀏覽器來源,逗號分隔。**空字串 = 完全不掛 CORS**(等同今天的行為)。

    為什麼是白名單而不是 `*`:認證用的是 httpOnly 的 refresh cookie + CSRF 雙送
    (見 auth.py),也就是 `allow_credentials=True`。而 `*` 配上 credentials 在
    規範上是無效的 —— 瀏覽器會直接拒絕,所以 `*` 不只是不安全,它根本不會動。
    非 DEBUG 下設 `*` 會拒絕啟動(見 _guard_cors_origins)。
    """

    MAX_REQUEST_BODY_BYTES: int = Field(default=1_048_576, gt=0)      # 1 MiB
    """請求體上限。超過回 413。

    1 MiB 遠大於這裡任何一個合法請求(最大的是 Stripe 的 webhook payload,幾十 KB),
    但足以擋掉「用大 body 把 512 MB 的任務推去 OOM」——那條路徑不需要認證,也不需要
    等候室的入場券。

    **刻意沒有「關掉」這個選項**(`gt=0`):0 在這裡的自然讀法是「拒絕所有帶 body 的
    請求」而不是「無限」,而一個 1 MiB 的上限也沒有任何需要臨時關掉的理由。
    """

    REQUEST_TIMEOUT_SECONDS: float = Field(default=15.0, ge=0)
    """單一請求「產生回應」的時限(不含之後的串流,見 RequestTimeoutMiddleware)。

    **0 = 完全不掛這層**(除錯時掛 debugger 用,跟 RATE_LIMIT_ENABLED 同一種逃生門)。
    負數會被拒絕:那是打錯字,而它的效果會是「護欄安靜地消失」。

    **必須小於 ALB 的 idle timeout(預設 60 秒)**:大於的話先放棄的是 ALB,客戶端拿到
    的 504 上面沒有 trace_id,而我們這邊的 log 什麼都沒有。也必須大於最慢的合法請求 ——
    這裡是建 Stripe PaymentIntent(外部網路往返)。
    """

    DB_STATEMENT_TIMEOUT_MS: int = 0
    """Postgres 的 `statement_timeout`,0 = 不設。**只有 API 該設**。

    預設 0 是刻意的:worker 的對帳/漂移偵測 cron 有合法的長查詢,而部署時的
    `alembic upgrade` 也用 worker 的 task def 跑(見 deploy.yml)—— 一個被砍掉的
    ALTER TABLE 比一個慢查詢糟得多。所以這個值由 api 的 task def 單獨打開。

    它跟 REQUEST_TIMEOUT_SECONDS 是兩件事,而且**應該設得比它小**:逾時只是放棄等待,
    查詢還在資料庫裡跑;statement_timeout 才真的把它殺掉、把連線還回池子。
    """

    AUTO_HEAL_LOST_REDIS_STATE: bool = True
    """Redis 的庫存鍵不見了(節點被換、keyspace 被清、故障切換丟了尾端寫入)時,
    自動從 Postgres 重建。

    **預設開啟**,因為不修的後果比修錯嚴重得多:缺鍵會讓 `get_available` 回 0,於是
    那場**看起來完售**而所有計數器自洽 —— 沒有任何訊號會亮,只有客訴。而重建的前提
    是無爭議的(backlog 為 0 時 Postgres 就是全部事實)。

    只治「鍵不存在」。鍵存在但值對不上是另一回事(可能是釋放路徑的 bug),那種只
    告警不自動修 —— 自動修會把 bug 的症狀每五分鐘擦掉一次,於是它永遠不會被查。
    """

    LOG_LEVEL: str = "INFO"
    """root logger 的層級。調成 DEBUG 是線上救火時的手段之一,所以它必須是設定而不是
    寫死的常數 —— 但**打錯字要當場炸**(見下面的 validator),不然 `LOG_LEVEL=INFOO`
    會讓 dictConfig 拋在啟動途中,而錯誤訊息跟「log 設定」看起來毫無關係。"""

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

    # 預熱擴容(publish_prewarm_signal → CloudWatch → api 的 scaling policy)。
    # 窗是 [sale_starts_at − LEAD, sale_starts_at + TAIL];窗內 `sale_imminent` 發 1。
    PREWARM_LEAD_MINUTES: int = Field(default=20, ge=1)
    """開賣前幾分鐘就把 api 拉到最大容量。

    **必須大於 `QUEUE_LEAD_TIME_SECONDS`**(等候室提前開放的時間),否則排隊一開放就
    有幾萬人掛上 SSE,而擴容還沒發生 —— 而那正是最需要容量的一刻。下面的 validator
    釘住這個關係。

    也要大於 ECS 拉起新任務的時間(拉 image + 啟動 + ALB 健康檢查通過,實測分鐘級),
    不然「預熱」會在尖峰進行到一半才完成。20 分鐘對兩者都有餘裕。
    """

    PREWARM_TAIL_MINUTES: int = Field(default=15, ge=1)
    """開賣後幾分鐘才把容量放掉。搶票的量在開賣後幾分鐘內就打完了,但「打完」不等於
    「可以立刻縮容」—— 縮太早會把還在付款/查詢座位的人打斷。"""

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

    @field_validator("LOG_LEVEL")
    @classmethod
    def _log_level_must_be_known(cls, v: str) -> str:
        level = v.strip().upper()
        if level not in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
            raise ValueError(
                f"unknown LOG_LEVEL {v!r}; expected one of "
                "CRITICAL/ERROR/WARNING/INFO/DEBUG"
            )
        return level

    @model_validator(mode="after")
    def _guard_prewarm_covers_the_queue(self) -> "Settings":
        # 預熱必須在等候室開放**之前**完成。反過來的話,排隊一開放就有人湧入,而
        # 擴容還沒開始 —— 預熱這個機制存在的唯一理由就沒了,但外觀上「我有預熱」。
        if self.PREWARM_LEAD_MINUTES * 60 <= self.QUEUE_LEAD_TIME_SECONDS:
            raise ValueError(
                f"PREWARM_LEAD_MINUTES ({self.PREWARM_LEAD_MINUTES}m) must exceed "
                f"QUEUE_LEAD_TIME_SECONDS ({self.QUEUE_LEAD_TIME_SECONDS}s) — the "
                "waiting room must not open before the capacity is there"
            )
        return self

    @model_validator(mode="after")
    def _guard_cors_origins(self) -> "Settings":
        # `*` 配 allow_credentials 在規範上無效(瀏覽器拒絕),所以它給人的是
        # 「我已經開好 CORS 了」的錯覺,實際上前端仍然壞掉、而且順手把來源限制拆了。
        # 跟其他旗標同一套 fail-closed:寧可拒絕啟動。
        if "*" in self.cors_allow_origins and not self.DEBUG:
            raise ValueError(
                "CORS_ALLOW_ORIGINS must list explicit origins when DEBUG=False; "
                "'*' is invalid with credentialed requests (the browser rejects it)"
            )
        return self

    @model_validator(mode="after")
    def _guard_inner_timeouts_are_shorter(self) -> "Settings":
        """每一個「內層」逾時都必須比請求逾時短。

        反過來的話,永遠是最外層先放棄,而內層那個設定等於不存在 —— 但兩個數字各自
        看起來都很合理。兩個具體後果:
          - statement_timeout 太長:請求被放棄,查詢仍在資料庫裡跑完,連線與 rows 還被
            佔著。
          - Stripe 逾時太長:webhook 的去重標記已經提交,而退款呼叫被外層砍掉 ——
            Stripe 重送會被當成處理過,那筆退款靜靜消失。
        """
        if self.REQUEST_TIMEOUT_SECONDS <= 0:
            return self             # 0 = 沒有外層逾時,沒有東西要比
        outer_ms = self.REQUEST_TIMEOUT_SECONDS * 1000
        inner_ms = {
            "DB_STATEMENT_TIMEOUT_MS": self.DB_STATEMENT_TIMEOUT_MS,
            "STRIPE_TIMEOUT_SECONDS": self.STRIPE_TIMEOUT_SECONDS * 1000,
        }
        for name, value in inner_ms.items():
            if value >= outer_ms:
                raise ValueError(
                    f"{name} must be less than REQUEST_TIMEOUT_SECONDS "
                    f"({self.REQUEST_TIMEOUT_SECONDS}s) — otherwise the request is "
                    f"abandoned first and {name} never takes effect"
                )
        return self

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
    def cors_allow_origins(self) -> list[str]:
        """`CORS_ALLOW_ORIGINS` 拆成清單。空的代表不掛 CORS middleware。

        用逗號分隔的字串而不是 pydantic 的 list 型別:這個值來自 ECS task def 的
        環境變數,而 pydantic 會把 list 欄位當 JSON 解析 —— 那表示 task def 裡得寫
        `["https://a","https://b"]`,一個引號打錯就是啟動失敗。
        """
        return [origin.strip() for origin in self.CORS_ALLOW_ORIGINS.split(",") if origin.strip()]

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