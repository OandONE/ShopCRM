from sqlmodel import SQLModel, Field
from pydantic import NaiveDatetime
from typing import Optional
from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """زمان UTC بدون timezone - برای ذخیره در SQLite"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Admin(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive)


class AdminSession(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    token: str = Field(unique=True, index=True)
    admin_id: int = Field(foreign_key="admin.id", index=True)
    csrf_token: str
    expires_at: NaiveDatetime
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive)


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
    name: str = Field(unique=True, index=True)


class Sale(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive, index=True)
    note: Optional[str] = None


class SaleValue(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    sale_id: int = Field(foreign_key="sale.id", index=True)
    column_id: int = Field(foreign_key="column.id", index=True)
    value: str
