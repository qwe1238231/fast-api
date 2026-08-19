from fastapi import APIRouter
from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.orders import router as orders_router
from app.api.v1.buyer_info import router as buyer_info_router
from app.api.v1.webhook import router as webhooks_router
from app.api.v1.events import router as events_router
from app.api.v1.zones import router as zones_router


api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(orders_router)
api_router.include_router(buyer_info_router)
api_router.include_router(webhooks_router)
api_router.include_router(events_router)
api_router.include_router(zones_router)