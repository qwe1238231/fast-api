"""Domain exceptions — pure Python, no FastAPI / HTTP knowledge.

Translated to HTTP responses by handlers in app/api/exception_handlers.py.
"""


class DomainError(Exception):
    """Base for all business-logic errors."""

class EventError(DomainError):
    """Base for Event-related errors."""

class EventNotFound(EventError):
    def __init__(self, event_id: int):
        self.event_id = event_id
        super().__init__(f"Event {event_id} not found")

class EventNotOnSale(EventError):
    def __init__(self, event_id: int):
        self.event_id = event_id
        super().__init__(f"Event {event_id} is not on sale")

class EventCancelled(EventError):
    def __init__(self, event_id: int):
        self.event_id = event_id
        super().__init__(f"Event {event_id} is cancelled")

class ZoneRequired(EventError):
    """有座位圖的場次必須指定要買哪一區 —— 票價與配位都以 zone 為單位。"""
    def __init__(self, event_id: int):
        self.event_id = event_id
        super().__init__(f"Event {event_id} is seated; zone_id is required")

class ZoneNotForEvent(EventError):
    """這個 zone 不能用於這個場次。

    涵蓋四種情況,對外一律同一個錯誤:zone 不存在、屬於別的場館、這個場次沒設
    該區票價、或這個場次根本沒有座位圖。**別場館的 zone 是安全問題** —— 少了
    這道檢查,使用者可以帶另一個場館的便宜 zone_id 來買這場,而且 webhook 的
    金額驗證抓不到(total_price_cents 是照那個便宜價算的,前後一致)。
    """
    def __init__(self, event_id: int, zone_id: int, reason: str = "not sellable"):
        self.event_id = event_id
        self.zone_id = zone_id
        self.reason = reason
        super().__init__(f"Zone {zone_id} is not sellable for event {event_id}: {reason}")

class OrderError(DomainError):
    """Base for Order-related errors."""

class OrderNotFound(OrderError):
    def __init__(self, order_id: int):
        self.order_id = order_id
        super().__init__(f"Order {order_id} not found")

class OrderNotOwned(OrderError):
    def __init__(self, order_id: int):
        self.order_id = order_id
        super().__init__(f"Order {order_id} does not belong to current user")

class InvalidOrderTransition(OrderError):
    def __init__(self, order_id: int, from_status: str, to_status: str):
        self.order_id = order_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Order {order_id} cannot transition from {from_status} to {to_status}")

class DuplicateOrderRequest(OrderError):
    def __init__(self, idempotency_key: str):
        self.idempotency_key = idempotency_key
        super().__init__(f"Duplicate request with key {idempotency_key}")

class InventoryError(DomainError):
    """Base for inventory-related errors."""

class InventoryNotReconcilable(InventoryError):
    def __init__(self, event_id: int, backlog: int, dead_letter: int):
        self.event_id = event_id
        self.backlog = backlog
        self.dead_letter = dead_letter
        super().__init__(
            f"Cannot reconcile event {event_id}: order stream not drained "
            f"(backlog={backlog}, dead_letter={dead_letter}). "
            f"Drain the queue first, or use --force to override."
        )

class InsufficientInventory(InventoryError):
    def __init__(self, event_id: int, requested: int, available: int):
        self.event_id = event_id
        self.requested = requested
        self.available = available
        super().__init__(f"Event {event_id}: requested {requested}, only {available} available")

class AdmissionDenied(DomainError):
    """Order attempted without a valid waiting-room admission token."""
    def __init__(self, reason: str = "admission required"):
        self.reason = reason
        super().__init__(reason)

class RateLimited(DomainError):
    """Too many requests in the window."""
    def __init__(self, retry_after: int | None = None):
        self.retry_after = retry_after
        super().__init__("rate limit exceeded")

class BuyerInfoError(DomainError):
    """Base for buyer info errors."""

class BuyerInfoNotFound(BuyerInfoError):
    def __init__(self, user_id: int):
        self.user_id = user_id
        super().__init__(f"Buyer info not found for user {user_id}")
    
class BuyerInfoAlreadyExists(BuyerInfoError):
    def __init__(self, user_id: int):
        self.user_id = user_id
        super().__init__(f"User {user_id} already has buyer info")

class NationalIdAlreadyRegistered(BuyerInfoError):
    """Raised when national_id is already registered (by anyone).
    
    Generic message — don't reveal which national_id or which user."""
    def __init__(self):
        super().__init__(f"National ID is already registered")