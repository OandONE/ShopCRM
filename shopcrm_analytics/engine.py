"""Safe, database-side analytics query builder for ShopCRM

The engine deliberately uses allow-listed fields and SQLAlchemy expressions;
client-supplied identifiers are never interpolated into SQL strings
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import re
from typing import Any

from sqlalchemy import (
    Float,
    Integer,
    String,
    and_,
    case,
    cast,
    distinct,
    exists,
    func,
    literal,
    not_,
    or_,
    select,
)

from models import (
    Column,
    # Product,
    Sale,
    SaleItem,
    SaleItemValue,
    SaleReturn,
    SaleReturnItem,
    SaleValue,
    User,
)


MAX_FILTER_NODES = 80
MAX_FILTER_DEPTH = 6
MAX_FILTER_VALUE_LENGTH = 300
MAX_GROUPS = 2000

METRICS: dict[str, dict[str, Any]] = {
    "sales_count": {"label": "تعداد فاکتور", "kind": "integer", "purchase_cost": False},
    "items_count": {"label": "تعداد ردیف کالا", "kind": "integer", "purchase_cost": False},
    "quantity": {"label": "تعداد واحد فروخته‌شده", "kind": "integer", "purchase_cost": False},
    "gross_revenue": {"label": "مبلغ قبل از تخفیف‌های ردیف و فاکتور", "kind": "currency", "purchase_cost": False},
    "discount_total": {"label": "مجموع تخفیف", "kind": "currency", "purchase_cost": False},
    "refund_total": {"label": "مبلغ مرجوعی", "kind": "currency", "purchase_cost": False},
    "net_revenue": {"label": "درآمد خالص پس از مرجوعی", "kind": "currency", "purchase_cost": False},
    "average_sale_value": {"label": "میانگین مبلغ فاکتور پس از مرجوعی", "kind": "currency", "purchase_cost": False},
    "purchase_cost": {"label": "بهای خرید اقلام باقی‌مانده", "kind": "currency", "purchase_cost": True},
    "estimated_profit": {"label": "سود تخمینی اقلام دارای بهای خرید", "kind": "currency", "purchase_cost": True},
}

CHART_TYPES = {
    "bar", "line", "pie", "doughnut", "polarArea", "radar", "scatter", "bubble"
}

DATE_GROUPS = {
    "day": {"label": "روز", "format": "%Y-%m-%d"},
    "week": {"label": "هفته", "format": "%Y-W%W"},
    "month": {"label": "ماه", "format": "%Y-%m"},
    "year": {"label": "سال", "format": "%Y"},
}


class AnalyticsValidationError(ValueError):
    """An invalid analytics configuration supplied by the client"""


def _safe_text(value: Any, *, max_length: int = MAX_FILTER_VALUE_LENGTH) -> str:
    if value is None:
        return ""
    text_value = str(value).strip()
    if len(text_value) > max_length:
        raise AnalyticsValidationError("مقدار یکی از شرط‌ها بیش از حد طولانی است")
    return text_value


def _parse_date(value: Any, field_name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise AnalyticsValidationError(f"تاریخ «{field_name}» معتبر نیست")


def normalize_config(raw: Any) -> dict[str, Any]:
    """Validate and normalize the public analytics configuration."""
    if not isinstance(raw, dict):
        raise AnalyticsValidationError("ساختار درخواست تحلیل معتبر نیست")

    dataset = raw.get("dataset", "sales")
    if dataset not in {"sales", "items"}:
        raise AnalyticsValidationError("نوع داده انتخاب‌شده معتبر نیست")

    metric = raw.get("metric", "sales_count")
    if metric not in METRICS:
        raise AnalyticsValidationError("معیار انتخاب‌شده معتبر نیست")

    chart_type = raw.get("chart_type", "bar")
    if chart_type not in CHART_TYPES:
        raise AnalyticsValidationError("نوع نمودار پشتیبانی نمی‌شود")

    group_by = raw.get("group_by", "month")
    if not isinstance(group_by, str) or len(group_by) > 80:
        raise AnalyticsValidationError("گروه‌بندی انتخاب‌شده معتبر نیست")

    start_date = _parse_date(raw.get("date_from"), "از")
    end_date = _parse_date(raw.get("date_to"), "تا")
    if end_date < start_date:
        raise AnalyticsValidationError("تاریخ پایان نمی‌تواند قبل از تاریخ شروع باشد")
    if (end_date - start_date).days > 365 * 20:
        raise AnalyticsValidationError("بازهٔ زمانی بیش از ۲۰ سال است")

    filters = raw.get("filters") or {"logic": "AND", "children": []}
    if not isinstance(filters, dict):
        raise AnalyticsValidationError("ساختار فیلترها معتبر نیست")

    compare_enabled = bool(raw.get("compare_enabled", False))
    compare_from = compare_to = None
    if compare_enabled:
        compare_from = _parse_date(raw.get("compare_from"), "شروع بازهٔ مقایسه")
        compare_to = _parse_date(raw.get("compare_to"), "پایان بازهٔ مقایسه")
        if compare_to < compare_from:
            raise AnalyticsValidationError("پایان بازهٔ مقایسه نمی‌تواند قبل از شروع آن باشد")
        if (compare_to - compare_from).days > 365 * 20:
            raise AnalyticsValidationError("بازهٔ مقایسه بیش از ۲۰ سال است")

    return {
        "dataset": dataset,
        "metric": metric,
        "group_by": group_by,
        "chart_type": chart_type,
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "filters": filters,
        "compare_enabled": compare_enabled,
        "compare_from": compare_from.isoformat() if compare_from else None,
        "compare_to": compare_to.isoformat() if compare_to else None,
    }


def _column_maps(session) -> tuple[dict[int, Column], dict[int, Column]]:
    columns = session.execute(
    select(Column).order_by(Column.order, Column.id)
        ).scalars().all()
    return (
        {int(c.id): c for c in columns if c.scope == "sale"},
        {int(c.id): c for c in columns if c.scope == "item"},
    )


def _analytics_type(column_type: str) -> str:
    normalized = str(column_type or "").lower()
    if normalized in {"number", "integer", "float", "decimal", "currency"}:
        return "number"
    if normalized in {"date", "datetime"}:
        return "date"
    return "text"


def _numeric_type(column_type: str) -> bool:
    return _analytics_type(column_type) == "number"


def get_field_catalog(session, *, can_view_purchase_cost: bool) -> dict[str, Any]:
    """Return fields the UI may use; the query engine still validates them."""
    sale_columns, item_columns = _column_maps(session)
    fields = [
        {"id": "sale:id", "label": "شناسه فاکتور", "type": "number", "scope": "sale"},
        {"id": "sale:created_at", "label": "تاریخ فاکتور", "type": "date", "scope": "sale"},
        {"id": "sale:seller", "label": "ثبت‌کنندهٔ فاکتور", "type": "text", "scope": "sale"},
        {"id": "sale:note", "label": "یادداشت فاکتور", "type": "text", "scope": "sale"},
        {"id": "sale:discount_amount", "label": "تخفیف کل فاکتور", "type": "number", "scope": "sale"},
    ]
    fields.extend(
        {"id": f"sale_col:{c.id}", "label": c.name, "type": _analytics_type(c.type), "scope": "sale"}
        for c in sale_columns.values()
    )
    fields.extend([
        {"id": "item:product_name", "label": "نام کالای فاکتور", "type": "text", "scope": "item"},
        {"id": "item:quantity", "label": "تعداد", "type": "number", "scope": "item"},
        {"id": "item:manual_unit_price", "label": "قیمت ثبت‌شدهٔ واحد", "type": "number", "scope": "item"},
        {"id": "item:final_unit_price", "label": "قیمت واحد پس از تخفیف هر واحد", "type": "number", "scope": "item"},
        {"id": "item:discount_total", "label": "تخفیف کل ردیف", "type": "number", "scope": "item"},
    ])
    if can_view_purchase_cost:
        fields.append({"id": "item:purchase_unit_price", "label": "بهای خرید هر واحد", "type": "number", "scope": "item"})
    fields.extend(
        {"id": f"item_col:{c.id}", "label": c.name, "type": _analytics_type(c.type), "scope": "item"}
        for c in item_columns.values()
    )
    return {"fields": fields, "metrics": [
        {"id": key, **value} for key, value in METRICS.items()
        if can_view_purchase_cost or not value["purchase_cost"]
    ], "chart_types": sorted(CHART_TYPES)}


def _sale_value_expr(column_id: int):
    return (
        select(SaleValue.value)
        .where(SaleValue.sale_id == Sale.id, SaleValue.column_id == column_id)
        .limit(1)
        .scalar_subquery()
    )


def _item_value_expr(column_id: int):
    return (
        select(SaleItemValue.value)
        .where(SaleItemValue.item_id == SaleItem.id, SaleItemValue.column_id == column_id)
        .limit(1)
        .scalar_subquery()
    )


def _item_net_expr():
    legacy_total = (
        SaleItem.final_unit_price * SaleItem.quantity
        - func.coalesce(SaleItem.discount_total, 0)
        - func.coalesce(SaleItem.invoice_discount_share, 0)
    )
    return case(
        (SaleItem.final_line_total.is_not(None), SaleItem.final_line_total),
        else_=legacy_total,
    )


def _item_gross_expr():
    return func.coalesce(SaleItem.manual_unit_price, 0) * func.coalesce(SaleItem.quantity, 0)


def _item_refund_expr():
    return (
        select(func.coalesce(func.sum(SaleReturnItem.refund_amount), 0))
        .select_from(SaleReturnItem)
        .join(SaleReturn, SaleReturn.id == SaleReturnItem.return_id)
        .where(
            SaleReturnItem.sale_item_id == SaleItem.id,
            SaleReturn.accepted_by_seller.is_(True),
        )
        .scalar_subquery()
    )


def _item_restock_quantity_expr():
    return (
        select(func.coalesce(func.sum(SaleReturnItem.restock_quantity), 0))
        .select_from(SaleReturnItem)
        .join(SaleReturn, SaleReturn.id == SaleReturnItem.return_id)
        .where(
            SaleReturnItem.sale_item_id == SaleItem.id,
            SaleReturn.accepted_by_seller.is_(True),
        )
        .scalar_subquery()
    )


def _item_cost_expr():
    remaining_qty = func.max(
        0, func.coalesce(SaleItem.quantity, 0) - _item_restock_quantity_expr()
    )
    return case(
        (SaleItem.purchase_unit_price.is_not(None), SaleItem.purchase_unit_price * remaining_qty),
        else_=None,
    )


def _item_profit_expr():
    return case(
        (
            SaleItem.purchase_unit_price.is_not(None),
            _item_net_expr() - _item_refund_expr() - _item_cost_expr(),
        ),
        else_=None,
    )


def _sale_sum_item_expr(item_expression):
    return (
        select(func.coalesce(func.sum(item_expression), 0))
        .where(SaleItem.sale_id == Sale.id)
        .correlate(Sale)
        .scalar_subquery()
    )


def _sale_refund_expr():
    return (
        select(func.coalesce(func.sum(SaleReturn.total_refund), 0))
        .where(SaleReturn.sale_id == Sale.id, SaleReturn.accepted_by_seller.is_(True))
        .correlate(Sale)
        .scalar_subquery()
    )


def _sale_metric_component(metric: str):
    if metric == "items_count":
        return _sale_sum_item_expr(literal(1))
    if metric == "quantity":
        return _sale_sum_item_expr(SaleItem.quantity)
    if metric == "gross_revenue":
        return _sale_sum_item_expr(_item_gross_expr())
    if metric == "discount_total":
        # Invoice-level discount is stored once on Sale; per-unit and line
        # discounts are stored on each SaleItem. This avoids multiplying the
        # invoice discount when a sale contains several items
        item_discounts = _sale_sum_item_expr(
            func.coalesce(SaleItem.discount_per_unit, 0) * func.coalesce(SaleItem.quantity, 0)
            + func.coalesce(SaleItem.discount_total, 0)
        )
        return func.coalesce(Sale.discount_amount, 0) + item_discounts
    if metric == "refund_total":
        return _sale_refund_expr()
    if metric == "net_revenue":
        return _sale_sum_item_expr(_item_net_expr()) - _sale_refund_expr()
    if metric == "purchase_cost":
        return _sale_sum_item_expr(_item_cost_expr())
    if metric == "estimated_profit":
        return _sale_sum_item_expr(_item_profit_expr())
    raise AnalyticsValidationError("معیار برای این نوع تحلیل تعریف نشده است")


def _metric_aggregate(metric: str, dataset: str):
    if metric == "sales_count":
        return func.count(distinct(Sale.id))

    if dataset == "sales":
        if metric == "average_sale_value":
            net = _sale_metric_component("net_revenue")
            return func.sum(net) / func.nullif(func.count(distinct(Sale.id)), 0)
        component = _sale_metric_component(metric)
        return func.sum(component)

    # Item-level dataset: each SaleItem is one fact row. Correlated refunbd and
    # restock aggregates keep return joins from multiplying sale item rows
    if metric == "items_count":
        return func.count(SaleItem.id)
    if metric == "quantity":
        return func.sum(SaleItem.quantity)
    if metric == "gross_revenue":
        return func.sum(_item_gross_expr())
    if metric == "discount_total":
        return func.sum(
            func.coalesce(SaleItem.discount_per_unit, 0) * func.coalesce(SaleItem.quantity, 0)
            + func.coalesce(SaleItem.discount_total, 0)
            + func.coalesce(SaleItem.invoice_discount_share, 0)
        )
    if metric == "refund_total":
        return func.sum(_item_refund_expr())
    if metric == "net_revenue":
        return func.sum(_item_net_expr() - _item_refund_expr())
    if metric == "average_sale_value":
        return func.sum(_item_net_expr() - _item_refund_expr()) / func.nullif(func.count(distinct(Sale.id)), 0)
    if metric == "purchase_cost":
        return func.sum(_item_cost_expr())
    if metric == "estimated_profit":
        return func.sum(_item_profit_expr())
    raise AnalyticsValidationError("معیار انتخاب‌شده معتبر نیست")


def _static_field_expression(field_id: str):
    sale_fields = {
        "sale:id": (Sale.id, "number", "sale"),
        "sale:created_at": (Sale.created_at, "date", "sale"),
        "sale:note": (Sale.note, "text", "sale"),
        "sale:discount_amount": (Sale.discount_amount, "number", "sale"),
        "sale:seller": (
            select(User.username).where(User.id == Sale.created_by_id).limit(1).scalar_subquery(),
            "text", "sale"
        ),
    }
    item_fields = {
        "item:product_name": (SaleItem.product_name, "text", "item"),
        "item:quantity": (SaleItem.quantity, "number", "item"),
        "item:manual_unit_price": (SaleItem.manual_unit_price, "number", "item"),
        "item:final_unit_price": (SaleItem.final_unit_price, "number", "item"),
        "item:discount_total": (SaleItem.discount_total, "number", "item"),
        "item:purchase_unit_price": (SaleItem.purchase_unit_price, "number", "item"),
    }
    return sale_fields.get(field_id) or item_fields.get(field_id)


def _field_info(session, field_id: str, *, can_view_purchase_cost: bool):
    static = _static_field_expression(field_id)
    if static:
        if field_id == "item:purchase_unit_price" and not can_view_purchase_cost:
            raise AnalyticsValidationError("به اطلاعات بهای خرید دسترسی ندارید")
        return static

    match = re.fullmatch(r"(sale_col|item_col):(\d+)", field_id or "")
    if not match:
        raise AnalyticsValidationError("یکی از فیلترها به فیلد نامعتبر اشاره می‌کند")
    scope, raw_id = match.groups()
    column_id = int(raw_id)
    model = Column
    column = session.get(model, column_id)
    expected_scope = "sale" if scope == "sale_col" else "item"
    if not column or column.scope != expected_scope:
        raise AnalyticsValidationError("ستون سفارشی انتخاب‌شده پیدا نشد")
    if scope == "sale_col":
        return (_sale_value_expr(column_id), _analytics_type(column.type), "sale")
    return (_item_value_expr(column_id), _analytics_type(column.type), "item")


def _coerce_condition(expression, field_type: str, operator: str, raw_value: Any):
    if operator not in {"eq", "ne", "contains", "starts", "gt", "gte", "lt", "lte", "in", "between", "is_null", "not_null"}:
        raise AnalyticsValidationError("عملگر یکی از شرط‌ها پشتیبانی نمی‌شود")

    if operator in {"is_null", "not_null"}:
        return expression.is_(None) if operator == "is_null" else expression.is_not(None)

    def typed_value(value):
        value_text = _safe_text(value)
        if field_type == "number":
            try:
                return float(value_text)
            except ValueError:
                raise AnalyticsValidationError("برای این شرط باید مقدار عددی وارد کنید")
        return value_text

    if operator == "between":
        if not isinstance(raw_value, (list, tuple)) or len(raw_value) != 2:
            raise AnalyticsValidationError("برای شرط بین، باید دو مقدار وارد کنید")
        left, right = typed_value(raw_value[0]), typed_value(raw_value[1])
        if left > right:
            left, right = right, left
        if field_type == "number":
            expression = cast(expression, Float)
        elif field_type == "date":
            expression = func.date(expression)
        else:
            expression = cast(expression, String)
        return expression.between(left, right)

    if operator == "in":
        values_raw = raw_value
        if not isinstance(values_raw, (list, tuple)):
            values_raw = [x.strip() for x in _safe_text(values_raw).split(",") if x.strip()]
        values = [typed_value(value) for value in values_raw if _safe_text(value)]
        if not values:
            raise AnalyticsValidationError("فهرست مقادیر شرط خالی است")
        if len(values) > 100:
            raise AnalyticsValidationError("در هر شرط حداکثر ۱۰۰ مقدار مجاز است")
        if field_type == "number":
            expression = cast(expression, Float)
        elif field_type == "date":
            expression = func.date(expression)
        else:
            expression = cast(expression, String)
        return expression.in_(values)

    value = typed_value(raw_value)
    if field_type == "number":
        expression = cast(expression, Float)
    elif field_type == "date":
        expression = func.date(expression)

    if operator == "eq":
        return func.lower(cast(expression, String)) == str(value).lower() if field_type == "text" else expression == value
    if operator == "ne":
        return func.lower(cast(expression, String)) != str(value).lower() if field_type == "text" else expression != value
    if operator == "gt":
        return expression > value
    if operator == "gte":
        return expression >= value
    if operator == "lt":
        return expression < value
    if operator == "lte":
        return expression <= value
    if operator in {"contains", "starts"}:
        if field_type != "text":
            raise AnalyticsValidationError("جستجوی متنی فقط برای فیلدهای متنی ممکن است")
        escaped = str(value).lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%" if operator == "contains" else f"{escaped}%"
        return func.lower(cast(expression, String)).like(pattern, escape="\\")
    raise AnalyticsValidationError("عملگر یکی از شرط‌ها پشتیبانی نمی‌شود")


def _compile_filter_node(
    session,
    node: Any,
    dataset: str,
    *,
    can_view_purchase_cost: bool,
    sale_columns: dict[int, Column],
    item_columns: dict[int, Column],
    counters: dict[str, int],
    depth: int = 0,
):
    if depth > MAX_FILTER_DEPTH or not isinstance(node, dict):
        raise AnalyticsValidationError("ساختار گروه‌بندی شرط‌ها معتبر نیست")

    if "children" in node:
        children = node.get("children") or []
        if not isinstance(children, list):
            raise AnalyticsValidationError("شرط‌های گروه معتبر نیستند")
        if not children:
            return None
        logic = str(node.get("logic", "AND")).upper()
        if logic not in {"AND", "OR"}:
            raise AnalyticsValidationError("منطق گروه فقط می‌تواند AND یا OR باشد")
        parts = [
            _compile_filter_node(
                session, child, dataset,
                can_view_purchase_cost=can_view_purchase_cost,
                sale_columns=sale_columns, item_columns=item_columns,
                counters=counters, depth=depth + 1,
            )
            for child in children
        ]
        parts = [part for part in parts if part is not None]
        if not parts:
            return None
        combined = and_(*parts) if logic == "AND" else or_(*parts)
        return not_(combined) if node.get("negate") else combined

    counters["conditions"] += 1
    if counters["conditions"] > MAX_FILTER_NODES:
        raise AnalyticsValidationError("تعداد شرط‌ها بیش از حد مجاز است")

    field_id = _safe_text(node.get("field"), max_length=80)
    operator = str(node.get("op", "eq")).lower()
    info = _field_info(session, field_id, can_view_purchase_cost=can_view_purchase_cost)
    expression, field_type, scope = info
    condition = _coerce_condition(expression, field_type, operator, node.get("value"))

    if dataset == "sales" and scope == "item":
        # Item conditions on a sales-level analysis mean: the invoice has at
        # least one matching item. EXISTS avoids duplicate-count inflation.
        item_match = (
            select(literal(1))
            .select_from(SaleItem)
            .where(SaleItem.sale_id == Sale.id, condition)
            .correlate(Sale)
        )
        return exists(item_match)
    return condition


def _group_expression(session, group_by: str, dataset: str, sale_columns: dict[int, Column], item_columns: dict[int, Column]):
    if group_by == "none":
        return literal("همه")
    if group_by in {"day", "week", "month", "year"}:
        fmt = DATE_GROUPS[group_by]["format"]
        return func.strftime(fmt, Sale.created_at)
    if group_by == "quarter":
        year_part = func.strftime("%Y", Sale.created_at)
        month_number = cast(func.strftime("%m", Sale.created_at), Float)
        quarter_number = cast(cast((month_number - 1) / 3, Integer) + 1, String)
        return year_part + literal("-Q") + quarter_number
    if group_by == "product_name":
        if dataset != "items":
            raise AnalyticsValidationError("گروه‌بندی بر اساس کالا فقط در تحلیل اقلام ممکن است")
        return SaleItem.product_name
    if group_by == "seller":
        return select(User.username).where(User.id == Sale.created_by_id).limit(1).scalar_subquery()
    if group_by in {"gender", "age"}:
        needle = "جنسیت" if group_by == "gender" else "سن"
        candidates = [c for c in sale_columns.values() if needle in str(c.name)]
        if not candidates:
            raise AnalyticsValidationError(f"ستون مربوط به «{needle}» در تنظیمات فاکتور وجود ندارد")
        expression = _sale_value_expr(candidates[0].id)
        return cast(expression, Float) if group_by == "age" and _numeric_type(candidates[0].type) else expression
    match = re.fullmatch(r"field:(\d+)", group_by or "")
    if match:
        column_id = int(match.group(1))
        if column_id in sale_columns:
            expression = _sale_value_expr(column_id)
            return cast(expression, Float) if _numeric_type(sale_columns[column_id].type) else expression
        if dataset == "items" and column_id in item_columns:
            expression = _item_value_expr(column_id)
            return cast(expression, Float) if _numeric_type(item_columns[column_id].type) else expression
        raise AnalyticsValidationError("ستون انتخاب‌شده برای گروه‌بندی معتبر نیست")
    raise AnalyticsValidationError("گروه‌بندی انتخاب‌شده معتبر نیست")


def _period_where(start: date, end: date):
    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end + timedelta(days=1), time.min)
    return and_(Sale.created_at >= start_dt, Sale.created_at < end_dt)


def _execute_period(
    session,
    config: dict[str, Any],
    start: date,
    end: date,
    *,
    can_view_purchase_cost: bool,
):
    dataset = config["dataset"]
    metric = config["metric"]
    sale_columns, item_columns = _column_maps(session)

    if metric not in METRICS or (METRICS[metric]["purchase_cost"] and not can_view_purchase_cost):
        raise AnalyticsValidationError("به معیار انتخاب‌شده دسترسی ندارید")

    filters = _compile_filter_node(
        session, config["filters"], dataset,
        can_view_purchase_cost=can_view_purchase_cost,
        sale_columns=sale_columns, item_columns=item_columns,
        counters={"conditions": 0},
    )
    dimension = _group_expression(session, config["group_by"], dataset, sale_columns, item_columns)
    value_expression = _metric_aggregate(metric, dataset)

    if dataset == "items":
        from_clause = SaleItem.__table__.join(Sale.__table__, SaleItem.sale_id == Sale.id)
    else:
        from_clause = Sale.__table__

    conditions = [_period_where(start, end)]
    if filters is not None:
        conditions.append(filters)

    grouped_stmt = (
        select(dimension.label("label"), value_expression.label("value"))
        .select_from(from_clause)
        .where(*conditions)
        .group_by(dimension)
    )
    if config["group_by"] in {"day", "week", "month", "quarter", "year"}:
        grouped_stmt = grouped_stmt.order_by(dimension.asc())
    else:
        grouped_stmt = grouped_stmt.order_by(value_expression.desc())
    grouped_stmt = grouped_stmt.limit(MAX_GROUPS)

    total_stmt = (
        select(value_expression.label("value"))
        .select_from(from_clause)
        .where(*conditions)
    )
    rows = session.execute(grouped_stmt).all()
    total_row = session.execute(total_stmt).first()

    # Missing purchase prices are surfaced because profit/cost metrics cannot
    # be complete when historical lines did not snapshot their purchase price
    missing_cost_expr = func.sum(
        case((SaleItem.purchase_unit_price.is_(None), 1), else_=0)
    )
    if dataset == "items":
        missing_stmt = select(missing_cost_expr).select_from(from_clause).where(*conditions)
    else:
        missing_count_per_sale = _sale_sum_item_expr(
            case((SaleItem.purchase_unit_price.is_(None), 1), else_=0)
        )
        missing_stmt = select(func.sum(missing_count_per_sale)).select_from(from_clause).where(*conditions)
    missing_value = session.execute(missing_stmt).scalar()

    data_rows = []
    for row in rows:
        raw_label, raw_value = row[0], row[1]
        value = float(raw_value or 0)
        if METRICS[metric]["kind"] == "integer":
            value = int(round(value))
        data_rows.append({"label": str(raw_label) if raw_label is not None else "بدون مقدار", "value": value})

    total_value = float(total_row[0] or 0) if total_row else 0.0
    if METRICS[metric]["kind"] == "integer":
        total_value = int(round(total_value))

    return {
        "rows": data_rows,
        "total": total_value,
        "missing_purchase_cost_rows": int(missing_value or 0),
        "group_count": len(data_rows),
    }


def run_analysis(session, raw_config: Any, *, can_view_purchase_cost: bool) -> dict[str, Any]:
    config = normalize_config(raw_config)
    metric = config["metric"]
    if METRICS[metric]["purchase_cost"] and not can_view_purchase_cost:
        raise AnalyticsValidationError("به بهای خرید و سود تخمینی دسترسی ندارید")

    start = _parse_date(config["date_from"], "از")
    end = _parse_date(config["date_to"], "تا")
    current = _execute_period(session, config, start, end, can_view_purchase_cost=can_view_purchase_cost)

    comparison = None
    if config["compare_enabled"]:
        compare_start = _parse_date(config["compare_from"], "شروع بازهٔ مقایسه")
        compare_end = _parse_date(config["compare_to"], "پایان بازهٔ مقایسه")
        comparison = _execute_period(
            session, config, compare_start, compare_end,
            can_view_purchase_cost=can_view_purchase_cost,
        )
        old_value = comparison["total"]
        new_value = current["total"]
        difference = new_value - old_value
        percentage = None if old_value == 0 else (difference / abs(old_value)) * 100
        comparison["difference"] = difference
        comparison["percentage_change"] = percentage

    warnings = []
    if config["metric"] in {"purchase_cost", "estimated_profit"}:
        missing = current["missing_purchase_cost_rows"]
        if comparison:
            missing += comparison["missing_purchase_cost_rows"]
        if missing:
            warnings.append(
                f"برای {missing} ردیف کالا بهای خرید ثبت نشده است؛ محاسبهٔ بهای خرید و سود ممکن است کامل نباشد."
            )
    if current["group_count"] >= MAX_GROUPS:
        warnings.append(f"نمایش گروه‌ها به {MAX_GROUPS} مورد محدود شده است؛ جمع کل همچنان بر اساس همهٔ رکوردهای منطبق محاسبه شده است.")

    catalog = get_field_catalog(session, can_view_purchase_cost=can_view_purchase_cost)
    field_map = {field["id"]: field for field in catalog["fields"]}
    used_ids: set[str] = set()

    def collect_filter_fields(node):
        if not isinstance(node, dict):
            return
        if "children" in node:
            for child in node.get("children") or []:
                collect_filter_fields(child)
        elif node.get("field"):
            used_ids.add(str(node["field"]))

    collect_filter_fields(config["filters"])
    if config["group_by"] == "gender":
        candidates = [field for field in catalog["fields"] if field["scope"] == "sale" and "جنسیت" in field["label"]]
        used_ids.update(field["id"] for field in candidates[:1])
    elif config["group_by"] == "age":
        candidates = [field for field in catalog["fields"] if field["scope"] == "sale" and "سن" in field["label"]]
        used_ids.update(field["id"] for field in candidates[:1])
    elif config["group_by"].startswith("field:"):
        raw_id = config["group_by"].split(":", 1)[1]
        used_ids.update((f"sale_col:{raw_id}", f"item_col:{raw_id}"))
    elif config["group_by"] == "seller":
        used_ids.add("sale:seller")
    elif config["group_by"] == "product_name":
        used_ids.add("item:product_name")
    elif config["group_by"] in {"day", "week", "month", "quarter", "year"}:
        used_ids.add("sale:created_at")

    return {
        "schema": "shopcrm.analytics.v1",
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "config": config,
        "dataset_label": "فاکتورها" if config["dataset"] == "sales" else "اقلام فاکتورها",
        "group_by_label": config["group_by"],
        "field_definitions": [field_map[field_id] for field_id in sorted(used_ids) if field_id in field_map],
        "metric": {"id": metric, **METRICS[metric]},
        "current": {
            **current,
            "date_from": config["date_from"],
            "date_to": config["date_to"],
        },
        "comparison": ({
            **comparison,
            "date_from": config["compare_from"],
            "date_to": config["compare_to"],
        } if comparison else None),
        "warnings": warnings,
        "notes": [
            "تمام بازه‌های زمانی بر اساس زمان ذخیره‌شدهٔ فاکتور در SQLite محاسبه شده‌اند.",
            "داده‌های مشتری در این نسخه به فیلدهای هر فاکتور محدودند و شناسهٔ مشتری مستقل وجود ندارد.",
            "درآمد خالص برابر مبلغ نهایی اقلام منهای مبلغ مرجوعی تأییدشده است.",
        ],
    }
