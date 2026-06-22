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

class InsufficientInventory(InventoryError):
    def __init__(self, event_id: int, requested: int, available: int):
        self.event_id = event_id
        self.requested = requested
        self.available = available
        super().__init__(f"Event {event_id}: requested {requested}, only {available} available")

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