from pydantic import BaseModel


class PaymentIntentResponse(BaseModel):
    """Response from POST /v1/orders/{id}/payment-intent."""
    payment_intent_id: str
    client_secret: str
