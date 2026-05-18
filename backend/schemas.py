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


