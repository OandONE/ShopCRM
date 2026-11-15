from sqlmodel import SQLModel, Field
from pydantic import NaiveDatetime
from typing import Optional
from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """UTC بدون timezone - برای ذخیره در SQLite"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Users & Auth

class User(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    full_name: Optional[str] = None
    role: str = Field(default="admin")  # "owner" | "admin"
    is_active: bool = True
    is_deleted: bool = False
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive)

    # Permissions
    can_quick_sale: bool = True
    can_full_sale: bool = True
    can_search: bool = True
    can_view_reports: bool = False
    can_view_sales: bool = False
    can_manage_columns: bool = False
    can_manage_products: bool = True
    can_view_current_columns: bool = False

    def has_permission(self, name: str) -> bool:
        if self.role == "owner":
            return True
        if not self.is_active or self.is_deleted:
            return False
        return bool(getattr(self, f"can_{name}", False))

    def is_owner(self) -> bool:
        return self.role == "owner"


class UserSession(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    token: str = Field(unique=True, index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    csrf_token: str
    expires_at: NaiveDatetime
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive)


# Store settings

class StoreSettings(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    name: str = "ShopCRM"
    owner_name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    currency: str = "تومان"
    updated_at: NaiveDatetime = Field(default_factory=utcnow_naive)


# Audit log

class AuditLog(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    username_snapshot: str = ""
    action: str = Field(index=True)
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    metadata_json: Optional[str] = None
    success: bool = True
    error_message: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    accept_language: Optional[str] = None
    referer: Optional[str] = None
    timezone: Optional[str] = None
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive, index=True)


# Core data

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
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    note: Optional[str] = None


class SaleValue(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    sale_id: int = Field(foreign_key="sale.id", index=True)
    column_id: int = Field(foreign_key="column.id", index=True)
    value: str


class DeletedSale(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    original_sale_id: int = Field(index=True)
    data_json: str
    deleted_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    deleted_at: NaiveDatetime = Field(default_factory=utcnow_naive)
