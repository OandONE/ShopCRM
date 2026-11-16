from sqlmodel import SQLModel, Field
from pydantic import NaiveDatetime
from typing import Optional
from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Users & Auth

class User(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    full_name: Optional[str] = None
    role: str = Field(default="admin")
    is_active: bool = True
    is_deleted: bool = False
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive)

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


# Products (by price and history)

class Product(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    price: Optional[int] = None
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive)
    updated_at: NaiveDatetime = Field(default_factory=utcnow_naive)
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    updated_by_id: Optional[int] = Field(default=None, foreign_key="user.id")


class ProductPriceHistory(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    old_price: Optional[int] = None
    new_price: Optional[int] = None
    changed_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    changed_at: NaiveDatetime = Field(default_factory=utcnow_naive)


# Columns (by scope)

class Column(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    name: str
    type: str = "text"
    options: Optional[str] = None
    required: bool = False
    is_default: bool = False
    order: int = 0
    scope: str = "sale"  # "sale" | "item"


# Sales & Items

class Sale(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive, index=True)
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    note: Optional[str] = None


class SaleValue(SQLModel, table=True):
    """مقادیر ستون‌های scope=sale برای کل فاکتور"""
    id: int = Field(default=None, primary_key=True)
    sale_id: int = Field(foreign_key="sale.id", index=True)
    column_id: int = Field(foreign_key="column.id", index=True)
    value: str


class SaleItem(SQLModel, table=True):
    """هر قلم کالا در فاکتور"""
    id: int = Field(default=None, primary_key=True)
    sale_id: int = Field(foreign_key="sale.id", index=True)
    product_id: Optional[int] = Field(default=None, foreign_key="product.id")
    product_name: str
    quantity: int = 1
    db_unit_price: Optional[int] = None        # snapshot قیمت دیتابیس در لحظه فروش
    manual_unit_price: int = 0                  # قیمت وارد‌شده توسط فروشنده
    discount_per_unit: int = 0                  # تخفیف صریح هر واحد
    final_unit_price: int = 0                   # manual - discount (محاسبه و ذخیره)


class SaleItemValue(SQLModel, table=True):
    """مقادیر ستون‌های scope=item (برای آینده)"""
    id: int = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="saleitem.id", index=True)
    column_id: int = Field(foreign_key="column.id", index=True)
    value: str


class DeletedSale(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    original_sale_id: int = Field(index=True)
    data_json: str
    deleted_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    deleted_at: NaiveDatetime = Field(default_factory=utcnow_naive)
    