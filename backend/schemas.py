from pydantic import BaseModel , ConfigDict , Field
from typing import Optional
from datetime import datetime

class UserCreate(BaseModel):
    username:str
    password:str

class UserResponse(BaseModel):
    id:int
    username:str
    is_active:bool
    model_config = ConfigDict(from_attributes=True) 

class Token(BaseModel):
    access_token:str
    token_type:str

class BookBase(BaseModel):
    title: str  
    author:str 
    is_active:bool=True
    price:float=0.0
    description:Optional[str]=None
class BookCreate(BookBase):
    pass

class BookUpdate(BookBase):
    title: Optional[str]=None
    author:Optional[str]=None
    is_active:Optional[bool]=None
    price:Optional[float]=None
    description:Optional[str]=None

class BookResponse(BookBase):
    id:int
    owner_id:int
    created_at:datetime
    update_at:Optional[datetime]=None
    model_config = ConfigDict(from_attributes=True)
