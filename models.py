from sqlmodel import SQLModel, Field
from typing import Optional


class Admin(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    username: str = Field(unique=True)
    password_hash: str


class Column(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    name: str
    type: str = "text"
    options: Optional[str] = None
    required: bool = False
    is_default: bool = False
    order: int = 0


class Product(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    name: str = Field(unique=True)


class SaleValue(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    sale_id: int
    column_id: int
    value: str
