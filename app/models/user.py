
from database import Base
from sqlalchemy.orm import Mapped,mapped_column

class User(Base):
    __tablename__="users"
    id:Mapped[int]=mapped_column(primary_key=True,index=True)
    username:Mapped[str]=mapped_column(unique=True,index=True)
    hashed_password:Mapped[str]=mapped_column()
    is_active:Mapped[bool]=mapped_column(default=True)

