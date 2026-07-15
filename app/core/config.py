from functools import lru_cache
from pydantic_settings import BaseSettings,SettingsConfigDict
from pydantic import field_validator
from datetime import timedelta
import base64
from pydantic import field_validator


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REDIS_URL: str = "redis://localhost:6380/0"
    DEBUG:bool = False
    REFRESH_TOKEN_SLIDING_DAYS: int = 14
    REFRESH_TOKEN_ABSOLUTE_EXPIRE_DAYS: int = 30
    REFRESH_TOKEN_REUSE_GRACE_SECONDS: int =10
    PII_KEK_BASE64: str
    PII_LOOKUP_KEY_BASE64:str
    STRIPE_SECRET_KEY: str
    STRIPE_WEBHOOK_SECRET: str=""
    LOGIN_RATE_LIMIT: str = "5/minute"
    AUDIT_LOG_RETENTION_DAYS: int = 90

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
    QUEUE_ADMISSION_BUFFER_SECONDS: int = 30    # registration closes / admission begins this long before sale
    QUEUE_ADMISSION_RATE: int = 500             # users admitted per second (the gatekeeper throttle)
    QUEUE_ADMISSION_TOKEN_TTL_SECONDS: int = 120  # admitted buyers must complete within this window
    QUEUE_JOIN_LIMIT_PER_MINUTE: int = 30         # anti-hammer cap on queue-join per user per event
    # Circuit breaker: pause admission when the downstream order pipeline is unhealthy.
    ADMISSION_PAUSE_DEAD_LETTER_THRESHOLD: int = 100   # dead-lettered orders above this → pause
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

    @property
    def access_token_lifetime(self) -> timedelta:
        return timedelta(minutes=self.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    @property
    def access_token_lifetime_seconds(self) -> int:
        return int(self.access_token_lifetime.total_seconds())

@lru_cache
def get_settings() -> Settings:
    return Settings()