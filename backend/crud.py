from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_
import models,schemas,security

async def get_user_by_username(db:AsyncSession,username:str):
    result = await db.execute(select(models.User).where(models.User.username==username))
    return result.scalars().first()

async def create_user(db:AsyncSession,user:schemas.UserCreate):
    hashed_password = security.get_password_hash(user.password)
    db_user = models.User(username=user.username,hashed_password=hashed_password)
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return db_user

async def create_book(db:AsyncSession, book:schemas.BookCreate,user_id:int):
    new_book=models.Book(**book.model_dump(),owner_id=user_id)
    db.add(new_book)
    await db.commit()
    await db.refresh(new_book)
    return new_book


async def read_book(db:AsyncSession,book_id:int):
    result = await db.execute(select(models.Book).where(models.Book.id==book_id))
    book = result.scalars().first()
    if book is None:
        raise HTTPException(status_code=404,detail="Book not found")
    return book

async def read_books(
    db:AsyncSession,
    skip:int=0,
    limit:int=10,
    q:str|None=None,
    ):
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

async def update_books(db:AsyncSession,book_id:int,book_update:schemas.BookCreate):
    book = await read_book(db, book_id)
    if book.owner_id != book_update.owner_id:
        raise HTTPException(status_code=403,detail="Not authorized to update this book")
    update_data=book_update.model.dump(exclude_unset=True)
    for key,value in update_data.items():
        setattr(book,key,value)

    await db.commit()
    await db.refresh(book)
    return book

async def delete_book(db:AsyncSession,book_id:int,user_id:int):
    book= await read_book(db, book_id)
    if book.owner_id != user_id:
        raise HTTPException(status_code=403,detail="Not authorized to delete this book")
    await db.delete(book)
    await db.commit()
    return
