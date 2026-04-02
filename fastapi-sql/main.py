from fastapi import FastAPI, HTTPException,Depends,status,Request
from typing import List,Annotated
from database import engine, Base
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_
from fastapi.responses import RedirectResponse
from jwt.exceptions import InvalidTokenError
from fastapi.security import OAuth2PasswordRequestForm
from slowapi import Limiter,_rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import crud,models,schemas,database,security
import jwt

limiter = Limiter(key_func=get_remote_address)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
@app.post("/users",response_model=schemas.UserResponse,status_code=status.HTTP_201_CREATED)
async def register(user:schemas.UserCreate,db:AsyncSession=Depends(database.get_db)):
    db_user = await crud.get_user_by_username(db, username=user.username)
    if db_user:
        raise HTTPException(status_code=400,detail="Username already registered")
    return await crud.create_user(db=db,user=user)

@app.post("/token",response_model=schemas.Token)
@limiter.limit("5/minute")
async def login_for_access_token(
    request:Request,
    form_data:Annotated[OAuth2PasswordRequestForm,Depends()],
    db:AsyncSession=Depends(database.get_db)
    ):
    user= await crud.get_user_by_username(db,username=form_data.username)
    if not user or not security.verify_password(form_data.password,user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate":"Bearer"},
        )
    access_token = security.create_access_token(data={"sub":user.username})
    return{"access_token":access_token,"token_type":"bearer"}

async def get_current_user(
    token:Annotated[str,Depends(security.oauth2_scheme)],
    db:AsyncSession=Depends(database.get_db)
    ):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate":"Bearer"},
    )
    try:
        payload = jwt.decode(token,security.SECRET_KEY,algorithms=[security.ALGORITHM])
        username:str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except InvalidTokenError:
        raise credentials_exception
    user = await crud.get_user_by_username(db,username=username)
    if user is None or not user.is_active:
        raise credentials_exception
    return user

@app.get("/users/me",response_model=schemas.UserResponse)
async def read_users_me(current_user:Annotated[models.User,Depends(get_current_user)]):
    return current_user

@app.get("/")
async def read_root():
    return RedirectResponse(url="/docs")

@app.post("/books",response_model=schemas.BookResponse,status_code=status.HTTP_201_CREATED)
async def create_book_api(book:schemas.BookCreate,db:AsyncSession=Depends(database.get_db),current_user:models.User=Depends(get_current_user)):
    return await crud.create_book(db=db,book=book,user_id=current_user.id)

@app.get("/books",response_model=List[schemas.BookResponse])
async def read_books(
    skip:int=0,
    limit:int=10,
    q:str|None=None,
    db:AsyncSession=Depends(database.get_db)):
    query=select(models.Book)
    if q:
        query=query.where(
            or_(
                models.Book.title.ilike(f"%{q}%"),
                models.Book.author.ilike(f"%{q}%")
            )
        )
    query=query.offset(skip).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()
   
    
    
@app.put("/books/{book_id}",response_model=schemas.BookResponse)
async def update_books_api(book_id:int,book_update:schemas.BookCreate,db:AsyncSession=Depends(database.get_db),current_user:models.User=Depends(get_current_user)):
    return await crud.update_books(db=db,book_id=book_id,book_update=book_update,user_id=current_user.id)

@app.delete("/books/{book_id}",status_code=status.HTTP_204_NO_CONTENT)
async def delete_book_api(book_id:int,db:AsyncSession=Depends(database.get_db),current_user:models.User=Depends(get_current_user)):
    await crud.delete_book(db=db,book_id=book_id,user_id=current_user.id)
    return  
