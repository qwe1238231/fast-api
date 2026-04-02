from datetime import datetime, timedelta, timezone
from typing import Union
import jwt
from pwdlib import PasswordHash
from fastapi.security import OAuth2PasswordBearer

SECRET_KEY = "0324c2c61d4a9555d11ad4c060d937cb40069110bf2d60f4c08d0d1a56693e5e"
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
