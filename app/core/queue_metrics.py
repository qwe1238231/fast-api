"""Prometheus collector for order-queue depth.

The worker / order-consumer processes aren't scraped by Prometheus — only the
API is. So the API exposes these gauges by reading Redis stream lengths at
scrape time via a small synchronous client (a couple of XLENs every 15s).

Registered on the default REGISTRY in app.main, so it shows up on /metrics
with no extra scrape target.
"""
from prometheus_client.core import GaugeMetricFamily
from redis import Redis as SyncRedis

from app.core.config import get_settings
from app.services.inventory import ORDER_STREAM_KEY, ORDER_DEAD_LETTER_KEY


class QueueDepthCollector:
    def __init__(self, redis_url: str | None = None) -> None:
        self._url = redis_url or get_settings().REDIS_URL
        self._client: SyncRedis | None = None

    def _redis(self) -> SyncRedis:
        if self._client is None:
            self._client = SyncRedis.from_url(self._url, decode_responses=True)
        return self._client

    def collect(self):
        r = self._redis()

        backlog = GaugeMetricFamily(
            "order_stream_backlog",
            "Unprocessed order intents waiting in orders:stream",
        )
        backlog.add_metric([], r.xlen(ORDER_STREAM_KEY))   # XLEN on a missing key -> 0
        yield backlog

        dead = GaugeMetricFamily(
            "order_dead_letter_depth",
            "Order intents given up on (should stay 0)",
        )
        dead.add_metric([], r.xlen(ORDER_DEAD_LETTER_KEY))
        yield dead
