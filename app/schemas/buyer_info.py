from typing import Annotated
from pydantic import BaseModel, StringConstraints


NationalId = Annotated[
    str,
    StringConstraints(
        min_length=10,
        max_length=10,
        pattern=r"^[A-Z][12]\d{8}$",
    ),
]


RealName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        strip_whitespace=True,
    ),
]


class BuyerInfoCreate(BaseModel):
    """Request body for POST /v1/buyer-info."""
    national_id: NationalId
    real_name: RealName


class BuyerInfoResponse(BaseModel):
    """Response body — returns plaintext to owner only."""
    real_name: str
    national_id: str
