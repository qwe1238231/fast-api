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


class UserResponse(BaseModel):
    """**不繼承 UserBase** —— username 在這裡是裸 str,不帶 Username 的 pattern。

    `^[a-zA-Z0-9_]+$` 是註冊時的**輸入**規則,不是資料庫裡那一欄的事實。回應模型
    套用輸入約束的話,任何一列不是經由註冊產生的資料都會讓端點噴
    ResponseValidationError(500)—— 而那是伺服器在說「我存的資料我自己不認得」。

    實際踩到的就是抹除:匿名化後的 username 是 `erased-user-<id>`,連字號是**刻意**
    選的,因為註冊規則不允許它 —— 沒有任何使用者能註冊出一個假的已抹除帳號,
    is_erased() 因此不可能誤判。約束留在入口,出口只負責忠實呈現。
    """

    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    is_active: bool
    