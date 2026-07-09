from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.services.stripe_client import create_stripe_client
from app.api.deps import limiter
from app.api.v1.router import api_router
from app.api.exception_handlers import register_exception_handlers

from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import REGISTRY
from app.core.queue_metrics import QueueDepthCollector


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.redis = create_redis_client(settings.REDIS_URL)
    app.state.stripe, app.state.stripe_http = create_stripe_client(settings.STRIPE_SECRET_KEY)
    yield
    await app.state.stripe_http.close_async()
    await app.state.redis.aclose()


app = FastAPI(
    title="Ticket System API",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
register_exception_handlers(app)
Instrumentator().instrument(app).expose(app)
REGISTRY.register(QueueDepthCollector())   # order_stream_backlog / order_dead_letter_depth on /metrics
app.include_router(api_router, prefix="/v1")

@app.get("/", include_in_schema=False)
async def read_root():
    return RedirectResponse(url="/docs")