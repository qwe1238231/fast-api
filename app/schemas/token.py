from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class Token(BaseModel):
    """OAuth2 access token response (RFC 6749 $5.1)."""

    access_token: str
    token_type: Literal["bearer"]="bearer"
    expires_in: int
 

class TokenPayload(BaseModel):
    """Decoded JWT claims used internally for request authentication."""
    sub: str
    exp: datetime
    iat: datetime
    type: Literal["access"]