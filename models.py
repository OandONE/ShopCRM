from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint
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
    can_view_purchase_cost: bool = False

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
    return_deduction_percent: int = 15
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


# Products, stock and price history

class Product(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    price: Optional[int] = None
    purchase_price: Optional[int] = None
    stock_quantity: int = 0
    is_active: bool = True
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive)
    updated_at: NaiveDatetime = Field(default_factory=utcnow_naive)
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    updated_by_id: Optional[int] = Field(default=None, foreign_key="user.id")


class Category(SQLModel, table=True):
    """Product category managed by the store owner"""
    id: int = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True, max_length=100)
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive)
    updated_at: NaiveDatetime = Field(default_factory=utcnow_naive)
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    updated_by_id: Optional[int] = Field(default=None, foreign_key="user.id")


class ProductCategoryLink(SQLModel, table=True):
    """Many-to-many link between products and categories"""
    __table_args__ = (UniqueConstraint("product_id", "category_id", name="uq_product_category"),)

    id: int = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    category_id: int = Field(foreign_key="category.id", index=True)


class ProductPriceHistory(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    old_price: Optional[int] = None
    new_price: Optional[int] = None
    old_purchase_price: Optional[int] = None
    new_purchase_price: Optional[int] = None
    changed_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    changed_at: NaiveDatetime = Field(default_factory=utcnow_naive)


class BulkPriceUpdateOperation(SQLModel, table=True):
    """A single previewed and applied bulk price adjustment"""
    id: int = Field(default=None, primary_key=True)
    target_field: str
    change_type: str
    direction: str
    change_value: str
    filter_json: str
    matched_count: int = 0
    rolled_back_count: int = 0
    status: str = Field(default="applied", index=True)
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive, index=True)
    applied_at: NaiveDatetime = Field(default_factory=utcnow_naive)
    rolled_back_at: Optional[NaiveDatetime] = None


class BulkPriceUpdateItem(SQLModel, table=True):
    """Per-product before/after values for safe, selective rollback"""
    id: int = Field(default=None, primary_key=True)
    operation_id: int = Field(foreign_key="bulkpriceupdateoperation.id", index=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    old_value: int
    new_value: int
    price_history_id: Optional[int] = Field(default=None, foreign_key="productpricehistory.id")
    rolled_back: bool = False
    rollback_history_id: Optional[int] = Field(default=None, foreign_key="productpricehistory.id")


class InventoryMovement(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    quantity_change: int
    reason: str = Field(index=True)
    reference_type: Optional[str] = None
    reference_id: Optional[int] = None
    note: Optional[str] = None
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive, index=True)


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


# Customers and immutable wallet ledger

class Customer(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    full_name: str = Field(index=True)
    phone: Optional[str] = Field(default=None, index=True)
    address: Optional[str] = None
    notes: Optional[str] = None
    is_active: bool = True
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive, index=True)
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    updated_at: NaiveDatetime = Field(default_factory=utcnow_naive)


class WalletTransaction(SQLModel, table=True):
    """Append-only wallet ledger. Positive amounts credit the wallet; negative amounts debit it."""
    id: int = Field(default=None, primary_key=True)
    customer_id: int = Field(foreign_key="customer.id", index=True)
    amount: int
    kind: str = Field(index=True)
    reference_type: Optional[str] = None
    reference_id: Optional[int] = None
    note: Optional[str] = None
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive, index=True)


# Sales & Items

class Sale(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive, index=True)
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    note: Optional[str] = None
    discount_amount: int = 0
    customer_id: Optional[int] = Field(default=None, foreign_key="customer.id", index=True)
    # None means an older invoice created before payment tracking was introduced
    cash_paid: Optional[int] = None
    wallet_paid: int = 0


class SaleValue(SQLModel, table=True):
    """مقادیر ستون‌های scope=sale برای کل فاکتور"""
    id: int = Field(default=None, primary_key=True)
    sale_id: int = Field(foreign_key="sale.id", index=True)
    column_id: int = Field(foreign_key="column.id", index=True)
    value: str


class SaleItem(SQLModel, table=True):
    """هر قلم کالا در فاکتور؛ قیمت‌ها و نام به‌صورت snapshot نگهداری می‌شوند."""
    id: int = Field(default=None, primary_key=True)
    sale_id: int = Field(foreign_key="sale.id", index=True)
    product_id: Optional[int] = Field(default=None, foreign_key="product.id")
    product_name: str
    quantity: int = 1
    db_unit_price: Optional[int] = None
    purchase_unit_price: Optional[int] = None
    inventory_deducted: bool = False
    manual_unit_price: int = 0
    discount_per_unit: int = 0
    # تخفیف یک‌جای این ردیف (برای مجموع تعداد همان کالا)
    discount_total: int = 0
    # سهم این ردیف از تخفیف کل فاکتور؛ برای حفظ محاسبه تاریخی مرجوعی
    invoice_discount_share: int = 0
    # مبلغ نهایی این ردیف پس از همه تخفیف‌ها؛ برای داده‌های قدیمی NULL است
    final_line_total: Optional[int] = None
    final_unit_price: int = 0


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


# Returns: each return event is kept independently; partial and repeated returns are supported

class SaleReturn(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    sale_id: int = Field(foreign_key="sale.id", index=True)
    total_refund: int = 0
    note: Optional[str] = None
    accepted_by_seller: bool = True
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive, index=True)
    # NULL = awaiting owner decision; allowed values are "cash" and "wallet"
    refund_method: Optional[str] = None
    approved_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    approved_at: Optional[NaiveDatetime] = None


class SaleReturnItem(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    return_id: int = Field(foreign_key="salereturn.id", index=True)
    sale_item_id: int = Field(foreign_key="saleitem.id", index=True)
    quantity: int
    restock_quantity: int = 0
    deduction_percent: int = 15
    suggested_refund_amount: int = 0
    refund_amount: int = 0



# User-saved analytics configurations (aggregated results are recomputed on run)
class SavedAnalysis(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    name: str = Field(max_length=80)
    config_json: str
    created_at: NaiveDatetime = Field(default_factory=utcnow_naive)
    updated_at: NaiveDatetime = Field(default_factory=utcnow_naive, index=True)
