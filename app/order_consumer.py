"""Dedicated long-lived process that drains orders:stream in near-real-time.

Run: python -m app.order_consumer

Separate from the ARQ worker so it can be scaled independently (run several
replicas — the consumer group load-balances entries across them). The ARQ
worker keeps the reclaim/dead-letter safety net on its cron.
"""
import asyncio
import logging
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.redis import create_redis_client
from app.worker import (
    ensure_consumer_group,
    run_order_consumer_loop,
    ORDER_STREAM_KEY,
    ORDER_CONSUMER_GROUP,
)


logger = logging.getLogger(__name__)


async def main() -> None:
    configure_logging(component="ticket-consumer")
    redis = create_redis_client(get_settings().REDIS_URL)
    await ensure_consumer_group(redis, ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    logger.info("order consumer started", extra={"event": "consumer_started"})
    try:
        await run_order_consumer_loop(redis, stop_event=stop)
    finally:
        await redis.aclose()
        logger.info("order consumer stopped", extra={"event": "consumer_stopped"})


if __name__ == "__main__":
    asyncio.run(main())
