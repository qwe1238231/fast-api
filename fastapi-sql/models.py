from sqlalchemy import Column ,Integer ,String ,Boolean , Float , Text ,DateTime,ForeignKey
from sqlalchemy.sql import func
from database import Base
from typing import List
from sqlalchemy.orm import Mapped,mapped_column,relationship
from datetime import datetime

class User(Base):
    __tablename__="users"
    id:Mapped[int]=mapped_column(primary_key=True,index=True)
    username:Mapped[str]=mapped_column(unique=True,index=True)
    hashed_password:Mapped[str]=mapped_column()
    is_active:Mapped[bool]=mapped_column(default=True)
    books = relationship("Book", back_populates="owner")
class Book(Base):
    __tablename__ = "books"

    id : Mapped[int]=mapped_column(primary_key=True,index=True)
    title : Mapped[str]=mapped_column(String,index=True)
    author : Mapped[str]=mapped_column(String,index=True)
    is_active : Mapped[bool]=mapped_column(Boolean,default=True)
    price : Mapped[float]=mapped_column(Float,default=0.0)
    description : Mapped[str|None]=mapped_column(Text)
    publisher : Mapped[str|None]=mapped_column(String)
    created_at : Mapped[datetime]=mapped_column(server_default=func.now())
    update_at : Mapped[datetime]=mapped_column(server_default=func.now(),onupdate=func.now())
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
    owner = relationship("User", back_populates="books")
