"""Redis async client factory.

Single client instance per process; lifecycle managed by FastAPI lifespan.
"""
from redis.asyncio import Redis, from_url


def create_redis_client(url: str) -> Redis:
    """Build an async Redis client.
    `decode_responses=True` makes the client return str instead of bytes for
    string commands — saves manual .decode() calls everywhere.
    """
    return from_url(
        url,
        encoding="utf-8",
        decode_responses=True,
    )