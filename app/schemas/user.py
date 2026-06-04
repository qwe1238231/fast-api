from typing import Annotated

from pydantic import BaseModel,ConfigDict,Field,SecretStr,StringConstraints

Username = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_]+$",
        strip_whitespace=True,
    ),
]
Password = Annotated[
    SecretStr,
    Field(
        min_length=8,
        max_length=128,
    ),
]


class UserBase(BaseModel):
    """Share fields Not used directly as request/response."""

    username: Username


class UserCreate(UserBase):
    password: Password


class UserResponse(UserBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    is_active:bool
    