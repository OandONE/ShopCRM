from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Session, create_engine, select, func
from sqlalchemy import Float, cast, desc
from models import (
    User, UserSession, StoreSettings, AuditLog,
    Column, Product, ProductPriceHistory,
    Sale, SaleValue, SaleItem, SaleItemValue, DeletedSale,
)
from rate_limit import RateLimiter
from audit import write_audit
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import uvicorn, hashlib, hmac, secrets, io, csv, os, json, sqlite3


engine = create_engine("sqlite:///database.db", connect_args={"check_same_thread": False})
templates = Jinja2Templates(directory="templates")

SESSION_COOKIE = "shopcrm_session"
SESSION_DAYS = 7
login_limiter = RateLimiter(max_attempts=5, window_minutes=15, cleanup_minutes=30)

ITEM_FIELDS = {"product_name", "db_unit_price", "manual_unit_price", "discount_per_unit", "quantity"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith("pbkdf2$"):
        _, salt_hex, digest_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return hmac.compare_digest(expected.hex(), digest_hex)
    return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored)


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_session(request: Request) -> UserSession | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with Session(engine) as s:
        return s.exec(
            select(UserSession).where(
                UserSession.token == token,
                UserSession.expires_at > utcnow(),
            )
        ).first()


def get_current_user(request: Request) -> User | None:
    if hasattr(request.state, "current_user"):
        return request.state.current_user
    sess = get_session(request)
    if not sess:
        request.state.current_user = None
        return None
    with Session(engine) as s:
        user = s.get(User, sess.user_id)
        if user and user.is_active and not user.is_deleted:
            request.state.current_user = user
            return user
    request.state.current_user = None
    return None


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def require_user(request: Request) -> User:
    user = get_current_user(request)
    if not user:
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        if _is_api(request):
            raise HTTPException(401, "نیاز به ورود")
        raise HTTPException(302, headers={"Location": f"/login?next={next_url}"})
    return user


def require_permission(name: str):
    def checker(request: Request) -> User:
        user = require_user(request)
        if not user.has_permission(name):
            if _is_api(request):
                raise HTTPException(403, "دسترسی ندارید")
            raise HTTPException(302, headers={"Location": "/"})
        return user
    return checker


def require_owner(request: Request) -> User:
    user = require_user(request)
    if not user.is_owner():
        if _is_api(request):
            raise HTTPException(403, "فقط مالک")
        raise HTTPException(302, headers={"Location": "/"})
    return user


def verify_csrf(request: Request, form_token: str) -> None:
    sess = get_session(request)
    if not sess or not form_token or not hmac.compare_digest(sess.csrf_token, form_token):
        raise HTTPException(403, "CSRF validation failed")


def verify_csrf_header(request: Request) -> None:
    sess = get_session(request)
    token = request.headers.get("x-csrf-token", "")
    if not sess or not token or not hmac.compare_digest(sess.csrf_token, token):
        raise HTTPException(403, "CSRF validation failed")


def _safe_next(next_url: str) -> str:
    if not next_url or not next_url.startswith("/"):
        return "/"
    if next_url.startswith("//") or "\\" in next_url:
        return "/"
    return next_url


def render(request: Request, name: str, status_code: int = 200, **ctx):
    user = get_current_user(request)
    sess = get_session(request)
    return templates.TemplateResponse(name, {
        "request": request,
        "current_user": user,
        "csrf_token": sess.csrf_token if sess else "",
        **ctx,
    }, status_code=status_code)


# Lifespan

@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for s in session.exec(select(UserSession).where(UserSession.expires_at < utcnow())).all():
            session.delete(s)

        if not session.exec(select(User)).first():
            session.add(User(
                username="owner",
                password_hash=hash_password("owner"),
                role="owner",
                full_name="مالک",
                is_active=True,
            ))
            print("✅ owner پیش‌فرض: owner / owner")

        if not session.exec(select(StoreSettings)).first():
            session.add(StoreSettings(name="ShopCRM"))

        if not session.exec(select(Column)).first():
            defaults = [
                Column(name="جنسیت خریدار", type="choice", options="مرد,زن",
                       is_default=True, order=0, scope="sale"),
                Column(name="سن حدودی خریدار", type="number",
                       is_default=False, order=1, scope="sale"),
                Column(name="قد حدودی (سانت)", type="number",
                       is_default=False, order=2, scope="sale"),
                Column(name="توضیحات", type="textarea",
                       is_default=False, order=3, scope="sale"),
            ]
            for col in defaults:
                session.add(col)

        # Test Products
        if not session.exec(select(Product)).first():
            seed = [
                ("شیر", 150_000),
                ("پنیر", 310_000),
                ("ماست", 260_000),
                ("دوغ", 150_000),
                ("کره", 130_000),
                ("خامه", 115_000),
                ("عسل", 790_000),
                ("نان", 70_000),
                ("برنج", 2_700_000),
                ("روغن", 340_000),
            ]
            for name, price in seed:
                session.add(Product(name=name, price=price))

        session.commit()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Persian numbers

PERSIAN_DIGITS = ["صفر", "یک", "دو", "سه", "چهار", "پنج", "شش", "هفت", "هشت", "نه"]
PERSIAN_TEENS = ["ده", "یازده", "دوازده", "سیزده", "چهارده", "پانزده", "شانزده", "هفده", "هجده", "نوزده"]
PERSIAN_TENS = ["", "", "بیست", "سی", "چهل", "پنجاه", "شصت", "هفتاد", "هشتاد", "نود"]
PERSIAN_HUNDREDS = ["", "صد", "دویست", "سیصد", "چهارصد", "پانصد", "ششصد", "هفتصد", "هشتصد", "نهصد"]
PERSIAN_UNITS = ["", "هزار", "میلیون", "میلیارد", "تریلیون"]


def _below_1000(num: int) -> str:
    if num == 0:
        return ""
    parts = []
    if num >= 100:
        parts.append(PERSIAN_HUNDREDS[num // 100])
        num %= 100
    if 10 <= num < 20:
        parts.append(PERSIAN_TEENS[num - 10])
        num = 0
    elif num >= 20:
        parts.append(PERSIAN_TENS[num // 10])
        num %= 10
        if num > 0:
            parts.append(PERSIAN_DIGITS[num])
            num = 0
    elif num > 0:
        parts.append(PERSIAN_DIGITS[num])
    return " و ".join(parts)


def number_to_persian(n: int) -> str:
    if n == 0:
        return "صفر"
    if n < 0:
        return "منفی " + number_to_persian(-n)
    parts, unit_idx = [], 0
    while n > 0:
        part = n % 1000
        if part > 0:
            chunk = _below_1000(part)
            if unit_idx > 0 and unit_idx < len(PERSIAN_UNITS):
                chunk += " " + PERSIAN_UNITS[unit_idx]
            parts.append(chunk)
        n //= 1000
        unit_idx += 1
    return " و ".join(reversed(parts))


# Helpers

def _get_columns(session, default_only: bool = False, scope: str | None = None):
    q = select(Column).order_by(Column.order.asc())
    if default_only:
        q = q.where(Column.is_default == True)  # type: ignore
    if scope:
        q = q.where(Column.scope == scope)
    return session.exec(q).all()


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _to_int(v):
    try:
        return int(float(str(v)))
    except (ValueError, TypeError):
        return None


# Auth routes

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "", error: str = ""):
    user = get_current_user(request)
    safe_next = _safe_next(next)
    if user:
        return RedirectResponse(safe_next, status_code=303)
    return render(request, "login.html", error=error, next=safe_next)


@app.post("/login")
async def login(request: Request, next: str = ""):
    ip = _client_ip(request)
    if not login_limiter.is_allowed(ip):
        write_audit(engine, request, "user.login_failed", success=False,
                    error_message=f"rate limited ({ip})")
        return render(request, "login.html", status_code=429,
                      error="تعداد تلاش زیاد. کمی صبر کنید.",
                      next=_safe_next(next))

    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    session_token = None
    authed_user = None
    err = "نام کاربری یا رمز اشتباه"

    with Session(engine) as s:
        user = s.exec(select(User).where(User.username == username)).first()
        if user and user.is_deleted:
            err = "این حساب حذف شده"
        elif user and not user.is_active:
            err = "این حساب تعلیق شده"
        elif user and verify_password(password, user.password_hash):
            if not user.password_hash.startswith("pbkdf2$"):
                user.password_hash = hash_password(password)
            sess = UserSession(
                token=secrets.token_urlsafe(32),
                user_id=user.id,
                csrf_token=secrets.token_urlsafe(24),
                expires_at=utcnow() + timedelta(days=SESSION_DAYS),
            )
            s.add(sess)
            s.add(user)
            s.commit()
            session_token = sess.token
            authed_user = user

    if session_token and authed_user:
        login_limiter.reset(ip)
        write_audit(engine, request, "user.login", user=authed_user)
        resp = RedirectResponse(_safe_next(next), status_code=303)
        resp.set_cookie(SESSION_COOKIE, session_token,
                        httponly=True, samesite="lax",
                        max_age=SESSION_DAYS * 24 * 3600)
        return resp

    login_limiter.record(ip)
    write_audit(engine, request, "user.login_failed",
                username=username or "(empty)", success=False, error_message=err)
    return render(request, "login.html", status_code=401, error=err, next=_safe_next(next))


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    user = get_current_user(request)
    if token:
        with Session(engine) as s:
            sess = s.exec(select(UserSession).where(UserSession.token == token)).first()
            if sess:
                s.delete(sess)
                s.commit()
    if user:
        write_audit(engine, request, "user.logout", user=user)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/me/password", response_class=HTMLResponse)
def me_password_page(request: Request, user: User = Depends(require_user), msg: str = "", err: str = ""):
    return render(request, "me_password.html", msg=msg, err=err)


@app.post("/me/password")
async def me_password_submit(request: Request, user: User = Depends(require_user)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    current = str(form.get("current_password", ""))
    new_pw = str(form.get("new_password", ""))
    confirm = str(form.get("confirm_password", ""))

    if new_pw != confirm:
        return render(request, "me_password.html", status_code=400, err="رمز جدید و تکرارش یکسان نیستند")
    if len(new_pw) < 4:
        return render(request, "me_password.html", status_code=400, err="رمز جدید باید حداقل ۴ کاراکتر باشد")

    with Session(engine) as s:
        u = s.get(User, user.id)
        if not u or not verify_password(current, u.password_hash):
            write_audit(engine, request, "user.password_change", user=user, success=False,
                        error_message="current password wrong")
            return render(request, "me_password.html", status_code=400, err="رمز فعلی اشتباه است")
        u.password_hash = hash_password(new_pw)
        s.add(u)
        s.commit()

    write_audit(engine, request, "user.password_change", user=user, target_type="user", target_id=user.id)
    return RedirectResponse("/me/password?msg=ok", status_code=303)


# Dashboard

@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: User = Depends(require_user)):
    with Session(engine) as s:
        settings = s.exec(select(StoreSettings)).first()
    is_default_pw = False
    if user.role == "owner" and user.username == "owner":
        is_default_pw = verify_password("owner", user.password_hash)
    return render(request, "index.html", settings=settings, is_default_pw=is_default_pw)


# Sale forms

@app.get("/quick", response_class=HTMLResponse)
def quick_form(request: Request, user: User = Depends(require_permission("quick_sale"))):
    with Session(engine) as s:
        sale_cols = _get_columns(s, default_only=True, scope="sale")
        item_cols = _get_columns(s, scope="item")
    return render(request, "quick.html",
                  form_kind="quick", sale_columns=sale_cols, item_columns=item_cols)


@app.get("/full", response_class=HTMLResponse)
def full_form(request: Request, user: User = Depends(require_permission("full_sale"))):
    with Session(engine) as s:
        sale_cols = _get_columns(s, scope="sale")
        item_cols = _get_columns(s, scope="item")
    return render(request, "full.html",
                  form_kind="full", sale_columns=sale_cols, item_columns=item_cols)


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, user: User = Depends(require_permission("search"))):
    return render(request, "search.html")


# Admin page

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user: User = Depends(require_user)):
    has_any = (user.has_permission("manage_columns")
               or user.has_permission("manage_products")
               or user.has_permission("view_current_columns"))
    if not has_any:
        return RedirectResponse("/", status_code=303)
    with Session(engine) as s:
        columns = _get_columns(s) if (user.has_permission("view_current_columns")
                                       or user.has_permission("manage_columns")) else []
        products = s.exec(select(Product).order_by(Product.name)).all() if user.has_permission("manage_products") else []
        product_creators = {}
        for p in products:
            if p.created_by_id:
                u = s.get(User, p.created_by_id)
                if u:
                    product_creators[p.id] = u.username
    return render(request, "admin.html", columns=columns, products=products,
                  product_creators=product_creators)


# Reports

@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, user: User = Depends(require_permission("view_reports"))):
    return render(request, "reports.html")


@app.get("/api/reports/data")
def reports_data(request: Request, user: User = Depends(require_permission("view_reports"))):
    with Session(engine) as s:
        total_sales = s.exec(select(func.count(Sale.id))).one()
        total_items = s.exec(select(func.count(SaleItem.id))).one() or 0

        discount_sum = s.exec(
            select(func.sum(SaleItem.discount_per_unit * SaleItem.quantity))
        ).one() or 0

        revenue = s.exec(
            select(func.sum(SaleItem.final_unit_price * SaleItem.quantity))
        ).one() or 0

        # top products by qty
        rows = s.exec(
            select(SaleItem.product_name, func.sum(SaleItem.quantity))
            .group_by(SaleItem.product_name)
            .order_by(func.sum(SaleItem.quantity).desc())
            .limit(10)
        ).all()
        products = [{"name": r[0], "count": int(r[1] or 0)} for r in rows]

        # genders
        gender_col = s.exec(
            select(Column).where(Column.type == "choice", Column.scope == "sale")
        ).first()
        genders = []
        if gender_col:
            rows = s.exec(
                select(SaleValue.value, func.count(SaleValue.id))
                .where(SaleValue.column_id == gender_col.id)
                .group_by(SaleValue.value)
            ).all()
            genders = [{"name": r[0], "count": r[1]} for r in rows]

        return {
            "total_sales": total_sales,
            "total_items": total_items,
            "total_discount": int(discount_sum),
            "total_revenue": int(revenue),
            "products": products,
            "genders": genders,
        }


# Sales list & detail

def _enrich_sale(session, sale: Sale) -> dict:
    items = session.exec(select(SaleItem).where(SaleItem.sale_id == sale.id)).all()
    sale_values = session.exec(select(SaleValue).where(SaleValue.sale_id == sale.id)).all()
    creator = session.get(User, sale.created_by_id) if sale.created_by_id else None

    total_qty = sum(it.quantity for it in items)
    total_final = sum(it.final_unit_price * it.quantity for it in items)
    total_discount = sum(it.discount_per_unit * it.quantity for it in items)

    return {
        "id": sale.id,
        "created_at": sale.created_at,
        "values": items,
        "sale_values": sale_values,
        "creator_username": creator.username if creator else None,
        "total_qty": total_qty,
        "total_final": total_final,
        "total_discount": total_discount,
    }


@app.get("/sales", response_class=HTMLResponse)
def sales_page(request: Request, user: User = Depends(require_permission("view_sales"))):
    with Session(engine) as s:
        sale_columns = _get_columns(s, scope="sale")
        sales = s.exec(select(Sale).order_by(desc(Sale.id)).limit(100)).all()
        enriched = [_enrich_sale(s, sale) for sale in sales]
    return render(request, "sales.html", sales=enriched, sale_columns=sale_columns)


@app.get("/sales/{sale_id}", response_class=HTMLResponse)
def sale_detail(sale_id: int, request: Request, user: User = Depends(require_permission("view_sales"))):
    with Session(engine) as s:
        sale = s.get(Sale, sale_id)
        if not sale:
            raise HTTPException(404, "فاکتور پیدا نشد")
        data = _enrich_sale(s, sale)
        sale_columns = _get_columns(s, scope="sale")
        cols_by_id = {c.id: c.name for c in sale_columns}
    return render(request, "sale_detail.html", data=data, cols_by_id=cols_by_id)

# Invoice / Receipt

@app.get("/invoice/{sale_id}", response_class=HTMLResponse)
def invoice_page(sale_id: int, request: Request,
                 user: User = Depends(require_user)):
    with Session(engine) as s:
        sale = s.get(Sale, sale_id)
        if not sale:
            raise HTTPException(404, "فاکتور پیدا نشد")

        settings = s.exec(select(StoreSettings)).first()
        items = s.exec(select(SaleItem).where(SaleItem.sale_id == sale_id)).all()
        sale_values = s.exec(select(SaleValue).where(SaleValue.sale_id == sale_id)).all()
        sale_cols = {c.id: c.name for c in _get_columns(s, scope="sale")}

        total_qty      = sum(it.quantity for it in items)
        total_final    = sum(it.final_unit_price * it.quantity for it in items)
        total_discount = sum(it.discount_per_unit * it.quantity for it in items)
        total_words    = number_to_persian(total_final) + " تومان" if total_final else ""

        extras = [
            {"name": sale_cols.get(v.column_id, "?"), "value": v.value}
            for v in sale_values
        ]

    return render(request, "invoice.html",
                  sale=sale, items=items, extras=extras, settings=settings,
                  total_qty=total_qty, total_final=total_final,
                  total_discount=total_discount, total_words=total_words)

# Sales query & export

@app.post("/api/sales/query")
async def sales_query(request: Request, user: User = Depends(require_permission("view_sales"))):
    verify_csrf_header(request)
    body = await request.json()
    ids = _query_sale_ids(body)
    with Session(engine) as s:
        result = []
        for sid in ids[:200]:
            sale = s.get(Sale, sid)
            if not sale:
                continue
            data = _enrich_sale(s, sale)
            result.append({
                "sale_id": sale.id,
                "created_at": sale.created_at.isoformat(),
                "items_summary": "، ".join(f"{it.quantity}× {it.product_name}" for it in data["items"]),
                "total_qty": data["total_qty"],
                "total_final": data["total_final"],
                "total_discount": data["total_discount"],
            })
    return result


@app.post("/api/sales/export.csv")
async def sales_export(request: Request, user: User = Depends(require_permission("view_sales"))):
    verify_csrf_header(request)
    body = await request.json()
    ids = _query_sale_ids(body)
    with Session(engine) as s:
        sale_columns = _get_columns(s, scope="sale")
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf)
        header = ["شماره فاکتور", "تاریخ", "کالا", "تعداد",
                  "قیمت دیتابیس", "قیمت فروش", "تخفیف هر واحد", "مبلغ نهایی"]
        header += [c.name for c in sale_columns]
        writer.writerow(header)

        for sid in ids:
            sale = s.get(Sale, sid)
            if not sale:
                continue
            items = s.exec(select(SaleItem).where(SaleItem.sale_id == sid)).all()
            vals = {v.column_id: v.value for v in s.exec(
                select(SaleValue).where(SaleValue.sale_id == sid)
            ).all()}
            for it in items:
                row = [
                    sid,
                    sale.created_at.strftime("%Y-%m-%d %H:%M"),
                    it.product_name,
                    it.quantity,
                    it.db_unit_price if it.db_unit_price is not None else "",
                    it.manual_unit_price,
                    it.discount_per_unit,
                    it.final_unit_price,
                ]
                for c in sale_columns:
                    row.append(vals.get(c.id, ""))
                writer.writerow(row)

    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sales.csv"'},
    )


def _parse_iso_date(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def _query_sale_ids(body: dict) -> list[int]:
    from_date = _parse_iso_date(body.get("from_date", ""))
    to_date = _parse_iso_date(body.get("to_date", ""))
    filters = body.get("filters") or []

    with Session(engine) as s:
        stmt = select(Sale.id)
        if from_date:
            stmt = stmt.where(Sale.created_at >= from_date)
        if to_date:
            stmt = stmt.where(Sale.created_at < to_date + timedelta(days=1))
        result: set[int] = set(s.exec(stmt).all())

        for i, f in enumerate(filters):
            colId = f.get("colId")
            op = f.get("op", "=")
            val = str(f.get("value", ""))
            logic = (f.get("logic") or "AND").upper() if i > 0 else "AND"

            ids: set[int] = set()

            if isinstance(colId, str) and colId.startswith("item:"):
                field = colId.split(":", 1)[1]
                if field not in ITEM_FIELDS:
                    continue
                field_obj = getattr(SaleItem, field)
                vstmt = select(SaleItem.sale_id)
                if op == "=":
                    if field == "product_name":
                        vstmt = vstmt.where(field_obj == val)
                    else:
                        n = _to_int(val)
                        if n is not None:
                            vstmt = vstmt.where(field_obj == n)
                elif op == "like":
                    if field == "product_name":
                        vstmt = vstmt.where(field_obj.like(f"%{_escape_like(val)}%", escape="\\"))
                    else:
                        continue
                elif op in (">", "<", ">=", "<="):
                    if field == "product_name":
                        continue
                    try:
                        num = float(val)
                    except ValueError:
                        continue
                    col_num = cast(field_obj, Float)
                    vstmt = vstmt.where({
                        ">": col_num > num, "<": col_num < num,
                        ">=": col_num >= num, "<=": col_num <= num,
                    }[op])
                ids = set(s.exec(vstmt).all())
            else:
                try:
                    cid = int(colId)
                except (ValueError, TypeError):
                    continue
                vstmt = select(SaleValue.sale_id).where(SaleValue.column_id == cid)
                if op == "=":
                    vstmt = vstmt.where(SaleValue.value == val)
                elif op == "like":
                    vstmt = vstmt.where(SaleValue.value.like(f"%{_escape_like(val)}%", escape="\\"))
                elif op in (">", "<", ">=", "<="):
                    try:
                        num = float(val)
                    except ValueError:
                        continue
                    col_as_num = cast(SaleValue.value, Float)
                    vstmt = vstmt.where({
                        ">": col_as_num > num, "<": col_as_num < num,
                        ">=": col_as_num >= num, "<=": col_as_num <= num,
                    }[op])
                ids = set(s.exec(vstmt).all())

            if logic == "OR":
                result |= ids
            elif logic == "NOT":
                result -= ids
            else:
                result &= ids

            if not result:
                break

        return sorted(result, reverse=True)


# API: price words

@app.get("/api/price-to-words")
def price_to_words(price: str = "", user: User = Depends(require_user)):
    try:
        p = int(price)
    except (ValueError, TypeError):
        return {"words": ""}
    return {"words": number_to_persian(p) + " تومان"}


# API: products search (by price)

@app.get("/api/search-products")
def search_products(q: str = "", user: User = Depends(require_user)):
    with Session(engine) as s:
        if q:
            stmt = select(Product).where(Product.name.like(f"%{_escape_like(q)}%", escape="\\"))
            products = s.exec(stmt).all()
        else:
            products = s.exec(select(Product).order_by(Product.name)).all()
        return [{"id": p.id, "name": p.name, "price": p.price} for p in products]


# API: columns (by virtual item fields)

@app.get("/api/columns")
def list_columns(user: User = Depends(require_user)):
    with Session(engine) as s:
        cols = _get_columns(s)
        result = [
            {"id": c.id, "name": c.name, "type": c.type, "options": c.options, "scope": c.scope}
            for c in cols
        ]
    # Virtual fields for items
    result += [
        {"id": "item:product_name",      "name": "محصول",         "type": "text",   "options": None, "scope": "item"},
        {"id": "item:db_unit_price",     "name": "قیمت دیتابیس",  "type": "number", "options": None, "scope": "item"},
        {"id": "item:manual_unit_price", "name": "قیمت فروش",     "type": "number", "options": None, "scope": "item"},
        {"id": "item:discount_per_unit", "name": "تخفیف هر واحد", "type": "number", "options": None, "scope": "item"},
        {"id": "item:quantity",          "name": "تعداد",         "type": "number", "options": None, "scope": "item"},
    ]
    return result


# API: create invoice (substitute /api/sale)

@app.post("/api/sale/invoice")
async def create_invoice(request: Request, user: User = Depends(require_user)):
    verify_csrf_header(request)
    body = await request.json()

    source = body.get("source", "quick")
    if source not in ("quick", "full"):
        source = "quick"
    needed = "quick_sale" if source == "quick" else "full_sale"
    if not user.has_permission(needed):
        raise HTTPException(403, "دسترسی به این فرم را ندارید")

    raw_items = body.get("items") or []
    if not raw_items:
        raise HTTPException(400, "سبد خرید خالی است")

    sale_values_in = body.get("sale_values") or {}
    edited = bool(body.get("datetime_edited", False))
    created_at_str = body.get("created_at")

    if edited and created_at_str:
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", ""))
            if created_at.tzinfo is not None:
                created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, TypeError):
            raise HTTPException(400, "تاریخ نامعتبر")
    else:
        created_at = utcnow()

    validated = []
    with Session(engine) as s:
        all_columns = _get_columns(s)
        sale_cols = {c.id: c for c in all_columns if c.scope == "sale"}
        item_cols = {c.id: c for c in all_columns if c.scope == "item"}

        # validate required sale-scope
        for c in sale_cols.values():
            if c.required and not str(sale_values_in.get(str(c.id), "")).strip():
                raise HTTPException(400, f"فیلد «{c.name}» الزامی است")

        for raw in raw_items:
            try:
                pid = raw.get("product_id")
                pid = int(pid) if pid else None
                qty = int(raw.get("quantity", 1))
                manual = int(raw.get("manual_unit_price", 0))
                discount = int(raw.get("discount_per_unit", 0))
            except (ValueError, TypeError):
                raise HTTPException(400, "اطلاعات یکی از اقلام نامعتبر است")

            if qty < 1:
                raise HTTPException(400, "تعداد باید حداقل ۱ باشد")
            if manual < 0:
                raise HTTPException(400, "قیمت نمی‌تواند منفی باشد")
            if discount < 0:
                raise HTTPException(400, "تخفیف نمی‌تواند منفی باشد")
            if discount > manual:
                raise HTTPException(400, "تخفیف بیشتر از قیمت است")

            product = None
            db_price = None
            pname = ""
            if pid:
                product = s.get(Product, pid)
                if not product:
                    raise HTTPException(400, f"محصول #{pid} یافت نشد")
                db_price = product.price
                pname = product.name
            else:
                pname = str(raw.get("product_name", "")).strip()
                if not pname:
                    raise HTTPException(400, "نام کالا نمی‌تواند خالی باشد")

            final = manual - discount

            # custom item-scope values
            custom = {}
            for cid_str, v in (raw.get("custom_values") or {}).items():
                try:
                    cid = int(cid_str)
                except (ValueError, TypeError):
                    continue
                if cid in item_cols and str(v).strip():
                    custom[cid] = str(v).strip()

            # validate required item-scope
            for cid, c in item_cols.items():
                if c.required and not custom.get(cid):
                    raise HTTPException(400, f"فیلد «{c.name}» برای «{pname}» الزامی است")

            validated.append({
                "product_id": pid,
                "product_name": pname,
                "quantity": qty,
                "db_unit_price": db_price,
                "manual_unit_price": manual,
                "discount_per_unit": discount,
                "final_unit_price": final,
                "custom_values": custom,
            })

        sale = Sale(created_at=created_at, created_by_id=user.id)
        s.add(sale)
        s.commit()
        s.refresh(sale)

        for v in validated:
            custom = v.pop("custom_values", {})
            item = SaleItem(sale_id=sale.id, **v)
            s.add(item)
            s.commit()
            s.refresh(item)
            for cid, val in custom.items():
                s.add(SaleItemValue(item_id=item.id, column_id=cid, value=val))

        for cid_str, val in sale_values_in.items():
            try:
                cid = int(cid_str)
            except (ValueError, TypeError):
                continue
            v = str(val).strip()
            if v:
                s.add(SaleValue(sale_id=sale.id, column_id=cid, value=v))

        s.commit()
        sale_id = sale.id

    total_discount = sum(v["discount_per_unit"] * v["quantity"] for v in validated)
    write_audit(engine, request, "sale.create_invoice", user=user,
                target_type="sale", target_id=sale_id,
                metadata_json=json.dumps({
                    "items_count": len(validated),
                    "total_discount": total_discount,
                }, ensure_ascii=False))
    if edited:
        write_audit(engine, request, "sale.datetime_edited", user=user,
                    target_type="sale", target_id=sale_id,
                    metadata_json=json.dumps({"created_at": created_at.isoformat()}, ensure_ascii=False))
    if total_discount > 0:
        write_audit(engine, request, "sale.discount_given", user=user,
                    target_type="sale", target_id=sale_id,
                    metadata_json=json.dumps({"total_discount": total_discount}, ensure_ascii=False))

    return {
        "sale_id": sale_id,
        "redirect": f"/{source}",
        "invoice_url": f"/invoice/{sale_id}",
    }


# API: delete sale (by backup)

@app.post("/api/sale/delete")
async def delete_sale(request: Request, user: User = Depends(require_permission("view_sales"))):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    try:
        sid = int(str(form.get("sale_id")))
    except (ValueError, TypeError):
        raise HTTPException(400, "شناسه فروش نامعتبر است")

    with Session(engine) as s:
        sale = s.get(Sale, sid)
        if not sale:
            raise HTTPException(404, "فروش پیدا نشد")
        items = s.exec(select(SaleItem).where(SaleItem.sale_id == sid)).all()
        item_ids = [it.id for it in items]
        item_values = []
        if item_ids:
            item_values = s.exec(
                select(SaleItemValue).where(SaleItemValue.item_id.in_(item_ids))  # type: ignore
            ).all()
        vals = s.exec(select(SaleValue).where(SaleValue.sale_id == sid)).all()

        backup = {
            "sale": {
                "id": sale.id,
                "created_at": sale.created_at.isoformat(),
                "created_by_id": sale.created_by_id,
                "note": sale.note,
            },
            "items": [
                {
                    "product_id": it.product_id,
                    "product_name": it.product_name,
                    "quantity": it.quantity,
                    "db_unit_price": it.db_unit_price,
                    "manual_unit_price": it.manual_unit_price,
                    "discount_per_unit": it.discount_per_unit,
                    "final_unit_price": it.final_unit_price,
                } for it in items
            ],
            "item_values": [
                {"item_id": iv.item_id, "column_id": iv.column_id, "value": iv.value}
                for iv in item_values
            ],
            "values": [{"column_id": v.column_id, "value": v.value} for v in vals],
        }
        s.add(DeletedSale(
            original_sale_id=sid,
            data_json=json.dumps(backup, ensure_ascii=False),
            deleted_by_id=user.id,
        ))
        for iv in item_values:
            s.delete(iv)
        for it in items:
            s.delete(it)
        for v in vals:
            s.delete(v)
        s.delete(sale)
        s.commit()

    write_audit(engine, request, "sale.delete", user=user, target_type="sale", target_id=sid)
    return RedirectResponse("/sales", status_code=303)


# API: advanced search

@app.post("/api/search-advanced")
async def search_advanced(request: Request, user: User = Depends(require_permission("search"))):
    verify_csrf_header(request)
    body = await request.json()
    filters = body.get("filters", [])
    with Session(engine) as s:
        all_sales = s.exec(select(Sale.id)).all()
        result: set[int] | None = None

        for i, f in enumerate(filters):
            colId = f.get("colId")
            op = f.get("op", "=")
            val = str(f.get("value", ""))
            logic = (f.get("logic") or "AND").upper() if i > 0 else "AND"

            ids: set[int] = set()
            if isinstance(colId, str) and colId.startswith("item:"):
                field = colId.split(":", 1)[1]
                if field not in ITEM_FIELDS:
                    continue
                field_obj = getattr(SaleItem, field)
                vstmt = select(SaleItem.sale_id)
                if op == "=":
                    if field == "product_name":
                        vstmt = vstmt.where(field_obj == val)
                    else:
                        n = _to_int(val)
                        if n is not None:
                            vstmt = vstmt.where(field_obj == n)
                elif op == "like":
                    if field == "product_name":
                        vstmt = vstmt.where(field_obj.like(f"%{_escape_like(val)}%", escape="\\"))
                    else:
                        continue
                elif op in (">", "<", ">=", "<="):
                    if field == "product_name":
                        continue
                    try:
                        num = float(val)
                    except ValueError:
                        continue
                    col_num = cast(field_obj, Float)
                    vstmt = vstmt.where({
                        ">": col_num > num, "<": col_num < num,
                        ">=": col_num >= num, "<=": col_num <= num,
                    }[op])
                ids = set(s.exec(vstmt).all())
            else:
                try:
                    cid = int(colId)
                except (ValueError, TypeError):
                    continue
                vstmt = select(SaleValue.sale_id).where(SaleValue.column_id == cid)
                if op == "=":
                    vstmt = vstmt.where(SaleValue.value == val)
                elif op == "like":
                    vstmt = vstmt.where(SaleValue.value.like(f"%{_escape_like(val)}%", escape="\\"))
                elif op in (">", "<", ">=", "<="):
                    try:
                        num = float(val)
                    except ValueError:
                        continue
                    col_as_num = cast(SaleValue.value, Float)
                    vstmt = vstmt.where({
                        ">": col_as_num > num, "<": col_as_num < num,
                        ">=": col_as_num >= num, "<=": col_as_num <= num,
                    }[op])
                ids = set(s.exec(vstmt).all())

            if result is None:
                result = ids
            elif logic == "OR":
                result |= ids
            elif logic == "NOT":
                result -= ids
            else:
                result &= ids

            if result == set():
                break

        if result is None:
            result = set(all_sales)

        out = []
        for sid in sorted(result, reverse=True)[:100]:
            sale = s.get(Sale, sid)
            if not sale:
                continue
            items = s.exec(select(SaleItem).where(SaleItem.sale_id == sid)).all()
            vals = s.exec(select(SaleValue).where(SaleValue.sale_id == sid)).all()
            sale_cols = {c.id: c.name for c in _get_columns(s, scope="sale")}
            data = {sale_cols.get(v.column_id, "?"): v.value for v in vals}
            out.append({
                "sale_id": sid,
                "items_summary": "، ".join(f"{it.quantity}× {it.product_name}" for it in items),
                "data": data,
            })
        return out


# API: admin columns/products

@app.post("/api/column/add")
async def add_column(request: Request, user: User = Depends(require_permission("manage_columns"))):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    name = str(form.get("name", "")).strip() or "ستون جدید"
    scope = str(form.get("scope", "sale"))
    if scope not in ("sale", "item"):
        scope = "sale"
    with Session(engine) as s:
        if s.exec(select(Column).where(Column.name == name)).first():
            raise HTTPException(400, f"ستون با نام «{name}» وجود دارد")
        col = Column(
            name=name,
            type=str(form.get("type", "text")),
            options=str(form.get("options")) or None,
            required=form.get("required") == "on",
            is_default=form.get("is_default") == "on",
            order=len(s.exec(select(Column)).all()),
            scope=scope,
        )
        s.add(col)
        s.commit()
        cid = col.id
    write_audit(engine, request, "column.create", user=user, target_type="column", target_id=cid,
                metadata_json=json.dumps({"name": name, "scope": scope}, ensure_ascii=False))
    return RedirectResponse("/admin", status_code=303)


@app.post("/api/column/delete")
async def delete_column(request: Request, user: User = Depends(require_permission("manage_columns"))):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    try:
        cid = int(str(form.get("id")))
    except (ValueError, TypeError):
        raise HTTPException(400, "شناسه ستون نامعتبر است")
    with Session(engine) as s:
        col = s.get(Column, cid)
        if col:
            for v in s.exec(select(SaleValue).where(SaleValue.column_id == cid)).all():
                s.delete(v)
            for v in s.exec(select(SaleItemValue).where(SaleItemValue.column_id == cid)).all():
                s.delete(v)
            s.delete(col)
            s.commit()
    write_audit(engine, request, "column.delete", user=user, target_type="column", target_id=cid)
    return RedirectResponse("/admin", status_code=303)


@app.post("/api/product/add")
async def add_product(request: Request, user: User = Depends(require_permission("manage_products"))):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    name = str(form.get("name", "")).strip()
    price_str = str(form.get("price", "")).strip().replace(",", "")
    price = int(price_str) if price_str.isdigit() else None

    if not name:
        raise HTTPException(400, "نام محصول الزامی است")

    with Session(engine) as s:
        if s.exec(select(Product).where(Product.name == name)).first():
            raise HTTPException(400, f"محصول «{name}» قبلاً وجود دارد")
        p = Product(name=name, price=price,
                    created_by_id=user.id, updated_by_id=user.id)
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id

    write_audit(engine, request, "product.create", user=user, target_type="product", target_id=pid,
                metadata_json=json.dumps({"name": name, "price": price}, ensure_ascii=False))
    return RedirectResponse("/admin", status_code=303)


@app.post("/api/product/{pid}/update")
async def update_product(pid: int, request: Request, user: User = Depends(require_permission("manage_products"))):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    new_name = str(form.get("name", "")).strip()
    price_str = str(form.get("price", "")).strip().replace(",", "")
    new_price = int(price_str) if price_str.isdigit() else None

    if not new_name:
        raise HTTPException(400, "نام محصول الزامی است")

    with Session(engine) as s:
        p = s.get(Product, pid)
        if not p:
            raise HTTPException(404, "محصول پیدا نشد")
        dup = s.exec(select(Product).where(Product.name == new_name, Product.id != pid)).first()
        if dup:
            raise HTTPException(400, f"محصول دیگری با نام «{new_name}» وجود دارد")

        old_price = p.price
        price_changed = (old_price != new_price)

        p.name = new_name
        p.price = new_price
        p.updated_at = utcnow()
        p.updated_by_id = user.id
        s.add(p)

        if price_changed:
            s.add(ProductPriceHistory(
                product_id=pid,
                old_price=old_price,
                new_price=new_price,
                changed_by_id=user.id,
            ))
        s.commit()

    write_audit(engine, request, "product.update", user=user, target_type="product", target_id=pid)
    return RedirectResponse("/admin", status_code=303)


@app.post("/api/product/delete")
async def delete_product(request: Request, user: User = Depends(require_permission("manage_products"))):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    try:
        pid = int(str(form.get("id")))
    except (ValueError, TypeError):
        raise HTTPException(400, "شناسه محصول نامعتبر است")
    with Session(engine) as s:
        p = s.get(Product, pid)
        if p:
            s.delete(p)
            s.commit()
    write_audit(engine, request, "product.delete", user=user, target_type="product", target_id=pid)
    return RedirectResponse("/admin", status_code=303)


@app.get("/api/product/{pid}/history")
def product_history(pid: int, request: Request, user: User = Depends(require_user)):
    with Session(engine) as s:
        p = s.get(Product, pid)
        if not p:
            raise HTTPException(404, "محصول پیدا نشد")
        rows = s.exec(
            select(ProductPriceHistory)
            .where(ProductPriceHistory.product_id == pid)
            .order_by(desc(ProductPriceHistory.changed_at))
        ).all()
        result = []
        for r in rows:
            changer = s.get(User, r.changed_by_id) if r.changed_by_id else None
            result.append({
                "old_price": r.old_price,
                "new_price": r.new_price,
                "changed_by": changer.username if changer else "?",
                "changed_at": r.changed_at.isoformat(),
            })
        return result


# Owner routes

@app.get("/owner", response_class=HTMLResponse)
def owner_dashboard(request: Request, user: User = Depends(require_owner)):
    with Session(engine) as s:
        users_count = s.exec(select(func.count(User.id)).where(User.is_deleted == False)).one()  # type: ignore
        audit_count = s.exec(select(func.count(AuditLog.id))).one()
        deleted_count = s.exec(select(func.count(DeletedSale.id))).one()
    return render(request, "owner/dashboard.html",
                  users_count=users_count, audit_count=audit_count, deleted_count=deleted_count)


@app.get("/owner/users", response_class=HTMLResponse)
def owner_users(request: Request, user: User = Depends(require_owner)):
    with Session(engine) as s:
        users = s.exec(
            select(User).where(User.is_deleted == False).order_by(desc(User.created_at))
        ).all()
    return render(request, "owner/users.html", users=users)


@app.get("/owner/users/new", response_class=HTMLResponse)
def owner_user_new(request: Request, user: User = Depends(require_owner)):
    return render(request, "owner/user_form.html", mode="new", u=None)


@app.post("/api/owner/users/add")
async def owner_user_add(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    full_name = str(form.get("full_name", "")).strip() or None

    if not username or not password:
        raise HTTPException(400, "نام کاربری و رمز اجباری است")
    if len(password) < 4:
        raise HTTPException(400, "رمز باید حداقل ۴ کاراکتر باشد")

    with Session(engine) as s:
        if s.exec(select(User).where(User.username == username)).first():
            raise HTTPException(400, "این نام کاربری قبلاً استفاده شده")
        new_user = User(
            username=username,
            password_hash=hash_password(password),
            full_name=full_name,
            role="admin",
            is_active=True,
            can_quick_sale=form.get("can_quick_sale") == "on",
            can_full_sale=form.get("can_full_sale") == "on",
            can_search=form.get("can_search") == "on",
            can_view_reports=form.get("can_view_reports") == "on",
            can_view_sales=form.get("can_view_sales") == "on",
            can_manage_columns=form.get("can_manage_columns") == "on",
            can_manage_products=form.get("can_manage_products") == "on",
            can_view_current_columns=form.get("can_view_current_columns") == "on",
        )
        s.add(new_user)
        s.commit()
        new_id = new_user.id

    write_audit(engine, request, "user.create", user=user, target_type="user", target_id=new_id,
                metadata_json=json.dumps({"username": username}, ensure_ascii=False))
    return RedirectResponse("/owner/users", status_code=303)


@app.get("/owner/users/{user_id}/edit", response_class=HTMLResponse)
def owner_user_edit(user_id: int, request: Request, user: User = Depends(require_owner)):
    with Session(engine) as s:
        u = s.get(User, user_id)
    if not u or u.is_deleted:
        raise HTTPException(404, "کاربر پیدا نشد")
    return render(request, "owner/user_form.html", mode="edit", u=u)


@app.post("/api/owner/users/{user_id}/update")
async def owner_user_update(user_id: int, request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    with Session(engine) as s:
        u = s.get(User, user_id)
        if not u or u.is_deleted:
            raise HTTPException(404, "کاربر پیدا نشد")
        if u.role == "owner":
            raise HTTPException(400, "نمی‌توان مالک را اینجا ویرایش کرد")
        new_username = str(form.get("username", "")).strip()
        if not new_username:
            raise HTTPException(400, "نام کاربری نمی‌تواند خالی باشد")
        other = s.exec(select(User).where(User.username == new_username, User.id != user_id)).first()
        if other:
            raise HTTPException(400, "این نام کاربری قبلاً استفاده شده")
        u.username = new_username
        u.full_name = str(form.get("full_name", "")).strip() or None
        u.can_quick_sale = form.get("can_quick_sale") == "on"
        u.can_full_sale = form.get("can_full_sale") == "on"
        u.can_search = form.get("can_search") == "on"
        u.can_view_reports = form.get("can_view_reports") == "on"
        u.can_view_sales = form.get("can_view_sales") == "on"
        u.can_manage_columns = form.get("can_manage_columns") == "on"
        u.can_manage_products = form.get("can_manage_products") == "on"
        u.can_view_current_columns = form.get("can_view_current_columns") == "on"
        s.add(u)
        s.commit()
    write_audit(engine, request, "user.update", user=user, target_type="user", target_id=user_id)
    return RedirectResponse("/owner/users", status_code=303)


@app.post("/api/owner/users/{user_id}/toggle-active")
async def owner_user_toggle(user_id: int, request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    with Session(engine) as s:
        u = s.get(User, user_id)
        if not u or u.is_deleted:
            raise HTTPException(404, "کاربر پیدا نشد")
        if u.role == "owner":
            raise HTTPException(400, "مالک را نمی‌توان تعلیق کرد")
        u.is_active = not u.is_active
        s.add(u)
        if not u.is_active:
            for sess in s.exec(select(UserSession).where(UserSession.user_id == user_id)).all():
                s.delete(sess)
        s.commit()
        action = "user.activate" if u.is_active else "user.suspend"
    write_audit(engine, request, action, user=user, target_type="user", target_id=user_id)
    return RedirectResponse("/owner/users", status_code=303)


@app.post("/api/owner/users/{user_id}/delete")
async def owner_user_delete(user_id: int, request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    with Session(engine) as s:
        u = s.get(User, user_id)
        if not u or u.is_deleted:
            raise HTTPException(404, "کاربر پیدا نشد")
        if u.role == "owner":
            raise HTTPException(400, "مالک را نمی‌توان حذف کرد")
        u.is_deleted = True
        u.is_active = False
        s.add(u)
        for sess in s.exec(select(UserSession).where(UserSession.user_id == user_id)).all():
            s.delete(sess)
        s.commit()
    write_audit(engine, request, "user.delete", user=user, target_type="user", target_id=user_id)
    return RedirectResponse("/owner/users", status_code=303)


@app.post("/api/owner/users/{user_id}/reset-password")
async def owner_user_reset_pw(user_id: int, request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    new_pw = str(form.get("new_password", ""))
    if len(new_pw) < 4:
        raise HTTPException(400, "رمز باید حداقل ۴ کاراکتر باشد")
    with Session(engine) as s:
        u = s.get(User, user_id)
        if not u or u.is_deleted:
            raise HTTPException(404, "کاربر پیدا نشد")
        if u.role == "owner":
            raise HTTPException(400, "برای تغییر رمز مالک از /me/password استفاده کنید")
        u.password_hash = hash_password(new_pw)
        s.add(u)
        for sess in s.exec(select(UserSession).where(UserSession.user_id == user_id)).all():
            s.delete(sess)
        s.commit()
    write_audit(engine, request, "user.password_change", user=user, target_type="user", target_id=user_id)
    return RedirectResponse("/owner/users", status_code=303)


@app.get("/owner/settings", response_class=HTMLResponse)
def owner_settings(request: Request, user: User = Depends(require_owner)):
    with Session(engine) as s:
        settings = s.exec(select(StoreSettings)).first()
    return render(request, "owner/settings.html", settings=settings)


@app.post("/api/owner/settings/update")
async def owner_settings_update(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    name = str(form.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "نام فروشگاه اجباری است")
    with Session(engine) as s:
        settings = s.exec(select(StoreSettings)).first()
        if not settings:
            settings = StoreSettings(name=name)
        settings.name = name
        settings.owner_name = str(form.get("owner_name", "")).strip() or None
        settings.phone = str(form.get("phone", "")).strip() or None
        settings.address = str(form.get("address", "")).strip() or None
        settings.currency = str(form.get("currency", "")).strip() or "تومان"
        settings.updated_at = utcnow()
        s.add(settings)
        s.commit()
    write_audit(engine, request, "settings.update", user=user)
    return RedirectResponse("/owner/settings", status_code=303)


@app.get("/owner/audit", response_class=HTMLResponse)
def owner_audit(request: Request, user: User = Depends(require_owner),
                q: str = "", action: str = "", offset: int = 0):
    PAGE_SIZE = 100
    with Session(engine) as s:
        stmt = select(AuditLog).order_by(desc(AuditLog.created_at))
        if q:
            stmt = stmt.where(AuditLog.username_snapshot.like(f"%{_escape_like(q)}%", escape="\\"))
        if action:
            stmt = stmt.where(AuditLog.action == action)
        total = s.exec(select(func.count()).select_from(AuditLog)).one()
        logs = s.exec(stmt.offset(offset).limit(PAGE_SIZE)).all()
        actions = [r[0] for r in s.exec(
            select(AuditLog.action).distinct().order_by(AuditLog.action)
        ).all()]
    return render(request, "owner/audit.html",
                  logs=logs, total=total, offset=offset, page_size=PAGE_SIZE,
                  q=q, action=action, actions=actions)


@app.get("/owner/deleted-sales", response_class=HTMLResponse)
def owner_deleted_sales(request: Request, user: User = Depends(require_owner)):
    with Session(engine) as s:
        rows = s.exec(select(DeletedSale).order_by(desc(DeletedSale.deleted_at))).all()
        enriched = []
        for d in rows:
            try:
                data = json.loads(d.data_json)
            except Exception:
                data = {}
            items = data.get("items", [])
            enriched.append({
                "id": d.id,
                "original_sale_id": d.original_sale_id,
                "deleted_at": d.deleted_at,
                "items_summary": "، ".join(f"{it['quantity']}× {it['product_name']}" for it in items),
                "original_created_at": data.get("sale", {}).get("created_at"),
            })
    return render(request, "owner/deleted_sales.html", deleted=enriched)


@app.post("/api/owner/deleted-sales/{did}/restore")
async def owner_restore_sale(did: int, request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    with Session(engine) as s:
        d = s.get(DeletedSale, did)
        if not d:
            raise HTTPException(404, "رکورد پیدا نشد")
        try:
            data = json.loads(d.data_json)
        except Exception:
            raise HTTPException(400, "داده بک‌آپ خراب است")

        sale_info = data.get("sale", {})
        target_id = d.original_sale_id
        if s.get(Sale, target_id):
            target_id = None

        created_at = utcnow()
        if sale_info.get("created_at"):
            try:
                created_at = datetime.fromisoformat(sale_info["created_at"])
            except ValueError:
                pass

        sale = Sale(
            id=target_id,
            created_at=created_at,
            created_by_id=sale_info.get("created_by_id"),
            note=sale_info.get("note"),
        )
        s.add(sale)
        s.commit()
        s.refresh(sale)

        for it in data.get("items", []):
            s.add(SaleItem(
                sale_id=sale.id,
                product_id=it.get("product_id"),
                product_name=it.get("product_name", "?"),
                quantity=int(it.get("quantity", 1)),
                db_unit_price=it.get("db_unit_price"),
                manual_unit_price=int(it.get("manual_unit_price", 0)),
                discount_per_unit=int(it.get("discount_per_unit", 0)),
                final_unit_price=int(it.get("final_unit_price", 0)),
            ))

        for v in data.get("values", []):
            s.add(SaleValue(
                sale_id=sale.id,
                column_id=v["column_id"],
                value=v["value"],
            ))

        s.delete(d)
        s.commit()
        restored_id = sale.id

    write_audit(engine, request, "sale.restore", user=user, target_type="sale", target_id=restored_id)
    return RedirectResponse("/owner/deleted-sales", status_code=303)


# Backup

@app.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request, user: User = Depends(require_owner)):
    return render(request, "backup.html")


@app.get("/api/backup/download")
def download_backup(request: Request, user: User = Depends(require_owner)):
    write_audit(engine, request, "backup.download", user=user)
    return FileResponse("database.db", filename="backup.db")


@app.post("/api/backup/restore")
async def restore_backup(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    file = form["file"]
    if not hasattr(file, "read"):
        raise HTTPException(400, "فایل نامعتبر است")
    content = await file.read()  # type: ignore
    if not content:
        raise HTTPException(400, "فایل خالی است")

    tmp = "database.restore.tmp"
    with open(tmp, "wb") as f:
        f.write(content)
    try:
        conn = sqlite3.connect(tmp)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        conn.close()
    except sqlite3.DatabaseError:
        os.remove(tmp)
        raise HTTPException(400, "فایل بک‌آپ معتبر نیست")

    write_audit(engine, request, "backup.restore", user=user)

    engine.dispose()
    os.replace(tmp, "database.db")
    engine.dispose()
    SQLModel.metadata.create_all(engine)

    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
