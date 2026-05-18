from datetime import datetime, timedelta, timezone
from typing import Union
import jwt
import os
from pwdlib import PasswordHash
from fastapi.security import OAuth2PasswordBearer
SECRET_KEY = os.environ["SECRET_KEY"]
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

def verify_password(plain_password,hashed_password):
    return password_hash.verify(plain_password,hashed_password)

def get_password_hash(password):
    return password_hash.hash(password)

def create_access_token(data:dict,expires_delta:Union[timedelta,None]=None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp":expire})
    encoded_jwt = jwt.encode(to_encode,SECRET_KEY , algorithm=ALGORITHM)
    return encoded_jwt
