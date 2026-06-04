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