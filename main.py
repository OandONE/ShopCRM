from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Session, create_engine, select, func
from sqlalchemy import Float, cast, desc, update as sa_update
from models import (
    User, UserSession, StoreSettings, AuditLog,
    Column, Product, ProductPriceHistory, InventoryMovement,
    Sale, SaleValue, SaleItem, SaleItemValue, DeletedSale,
    SaleReturn, SaleReturnItem, SavedAnalysis,
)
from shopcrm_analytics import build_analytics_router
from rate_limit import RateLimiter
from audit import write_audit
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import uvicorn, hashlib, hmac, secrets, io, csv, os, json, sqlite3, re
from html import escape as html_escape


engine = create_engine("sqlite:///database.db", connect_args={"check_same_thread": False})
templates = Jinja2Templates(directory="templates")

SESSION_COOKIE = "shopcrm_session"
SESSION_DAYS = 7
login_limiter = RateLimiter(max_attempts=5, window_minutes=15, cleanup_minutes=30)

ITEM_FIELDS = {"product_name", "db_unit_price", "manual_unit_price", "discount_per_unit", "discount_total", "quantity"}


def _migrate_legacy_schema() -> None:
    """Additive SQLite migrations for databases created by older ShopCRM versions."""
    additions = {
        "user": {
            "can_view_purchase_cost": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "storesettings": {
            "return_deduction_percent": "INTEGER NOT NULL DEFAULT 15",
        },
        "product": {
            "purchase_price": "INTEGER",
            "stock_quantity": "INTEGER NOT NULL DEFAULT 0",
            "is_active": "BOOLEAN NOT NULL DEFAULT 1",
        },
        "productpricehistory": {
            "old_purchase_price": "INTEGER",
            "new_purchase_price": "INTEGER",
        },
        "sale": {
            "discount_amount": "INTEGER NOT NULL DEFAULT 0",
        },
        "saleitem": {
            "purchase_unit_price": "INTEGER",
            "inventory_deducted": "BOOLEAN NOT NULL DEFAULT 0",
            "discount_total": "INTEGER NOT NULL DEFAULT 0",
            "invoice_discount_share": "INTEGER NOT NULL DEFAULT 0",
            "final_line_total": "INTEGER",
        },
    }
    with engine.begin() as conn:
        for table, columns in additions.items():
            present = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info('{table}')").fetchall()}
            for column, ddl in columns.items():
                if column not in present:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


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


def _discount_breakdown_html(items, total_discount: int, invoice_discount: int) -> str:
    if not items or not total_discount:
        return ""
    rows = []
    for item in items:
        qty = int(item.quantity or 0)
        unit_discount = int(item.discount_per_unit or 0)
        line_discount = int(getattr(item, "discount_total", 0) or 0)
        invoice_share = int(getattr(item, "invoice_discount_share", 0) or 0)
        line_total = getattr(item, "final_line_total", None)
        if line_total is None:
            line_total = int(item.final_unit_price or 0) * qty - line_discount - invoice_share
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{:,}</td><td>{:,}</td><td>{:,}</td><td>{:,}</td></tr>".format(
                html_escape(str(item.product_name)), qty, unit_discount, line_discount,
                invoice_share, max(0, int(line_total or 0))
            )
        )
    return ("<section class='card shadow-sm mt-3 shopcrm-discount-breakdown'>"
            "<div class='card-header'>جزئیات تخفیف‌ها</div><div class='card-body'>"
            "<div class='table-responsive'><table class='table table-sm align-middle'><thead><tr>"
            "<th>کالا</th><th>تعداد</th><th>تخفیف هر واحد</th><th>تخفیف کل ردیف</th>"
            "<th>سهم از تخفیف فاکتور</th><th>مبلغ نهایی ردیف</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>"
            + "<div class='small text-body-secondary'>تخفیف کل فاکتور: {:,} تومان · مجموع تخفیف‌ها: {:,} تومان</div>".format(
                int(invoice_discount or 0), int(total_discount or 0)
            ) + "</div></section>")


def render(request: Request, name: str, status_code: int = 200, **ctx):
    user = get_current_user(request)
    sess = get_session(request)
    response = templates.TemplateResponse(name, {
        "request": request,
        "current_user": user,
        "csrf_token": sess.csrf_token if sess else "",
        **ctx,
    }, status_code=status_code)

    additions = []
    path = request.url.path

    if path == "/sales":
        additions.append("""<script>
        (() => {
          const clean = () => document.querySelectorAll('table').forEach(table => {
            if (table.dataset.shopcrmNoItemCol === '1') return;
            const headers = [...table.querySelectorAll('thead tr:last-child th')];
            const index = headers.findIndex(th => {
              const label = (th.innerText || th.textContent || '').trim();
              return label.startsWith('کالا') || label === 'محصول';
            });
            if (index < 0) return;
            const col = index + 1;
            const style = document.createElement('style');
            style.textContent = `table.shopcrm-no-item-col thead th:nth-child(${col}), table.shopcrm-no-item-col tbody td:nth-child(${col}), table.shopcrm-no-item-col tfoot td:nth-child(${col}) { display:none !important; }`;
            document.head.appendChild(style);
            table.classList.add('shopcrm-no-item-col');
            table.dataset.shopcrmNoItemCol = '1';
          });
          clean();
          const observer = new MutationObserver(clean);
          observer.observe(document.body, {childList:true, subtree:true});
          setTimeout(() => observer.disconnect(), 12000);
        })();
        </script>""")

    if path.startswith("/invoice/"):
        items = ctx.get("items") or []
        additions.append(_discount_breakdown_html(
            items, ctx.get("total_discount", 0), getattr(ctx.get("sale"), "discount_amount", 0)
        ))
    elif re.fullmatch(r"/sales/\d+", path):
        data = ctx.get("data") or {}
        items = data.get("items") or data.get("values") or []
        sale = data.get("sale")
        invoice_discount = data.get("invoice_discount", getattr(sale, "discount_amount", 0) if sale else 0)
        additions.append(_discount_breakdown_html(items, data.get("total_discount", 0), invoice_discount))

    if additions and response.body:
        body = response.body.decode("utf-8")
        snippet = "\n".join(x for x in additions if x)
        if "</body>" in body:
            body = body.replace("</body>", snippet + "\n</body>", 1)
        else:
            body += snippet
        response.body = body.encode("utf-8")
        response.headers["content-length"] = str(len(response.body))
    return response


# Lifespan

@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    _migrate_legacy_schema()
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
                ("مداد", 50_000, 50),
                ("دفتر 50 برگ سیمی 80 گرم", 150_000, 200),
                ("خودکار کیان", 35_000, 200),
                ("دفتر 100 برگ سیمی 80 گرم", 255_000, 200),
                ("تراش مخزن دار کرونا", 65_000, 20),
                ("خودکار X3 استایلیش", 49_000, 100),
                ("مداد فشنگی", 80_000, 30),
                ("یدک کلاسور 100 برگ", 290_000, 15),
                ("پاک کن کچل شو", 110_000, 15),
                ("اتود آفیس", 85_000, 20),
                ("اتود اونر", 125_000, 20),
                ("مغز اتود سی کلاس", 80_000, 100),
                ("مغز اتود کرونا", 50_000, 80),
                ("دفتر زبان 50 برگ دو خط فردیس", 130_000, 100),
                ("مقوا رنگی A4 بسته 10 عددی", 150_000, 30),
                ("باتری قلمی", 45_000, 100),
                ("باتری سکه ای 2032", 60_000, 10),
                ("ماژیک وایت برد صدف", 60_000, 50),
            ]
            for name, price, stock_quantity in seed:
                session.add(Product(name=name, price=price, stock_quantity=stock_quantity))

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

# Advanced analytics: a separate subsystem keeps query construction and UI
# logic out of the main application module
app.include_router(build_analytics_router(
    engine=engine,
    render=render,
    require_permission=require_permission,
    verify_csrf_header=verify_csrf_header,
))


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


def _parse_in_values(value):
    """Convert an IN filter value into a clean list of exact-match values"""
    if isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        text = str(value or "").strip()
        if not text:
            return []
        # Also accept a JSON array when a client sends one
        if text.startswith("[") and text.endswith("]"):
            try:
                decoded = json.loads(text)
                if isinstance(decoded, list):
                    raw_values = decoded
                else:
                    raw_values = [text]
            except (json.JSONDecodeError, TypeError):
                raw_values = re.split(r"[,،;\n\r]+", text)
        else:
            raw_values = re.split(r"[,،;\n\r]+", text)

    cleaned = []
    for item in raw_values:
        item = str(item).strip().strip('\"\'')
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


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


@app.get("/product-search", response_class=HTMLResponse)
def product_search_page(request: Request, user: User = Depends(require_permission("search"))):
    return render(request, "simple_search.html")


@app.get("/shortcuts", response_class=HTMLResponse)
def shortcuts_page(request: Request, user: User = Depends(require_user)):
    return render(request, "shortcuts.html")


# Admin page

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user: User = Depends(require_user)):
    has_any = (user.has_permission("manage_columns")
               or user.has_permission("manage_products")
               or user.has_permission("view_current_columns")
               or user.has_permission("view_sales"))
    if not has_any:
        return RedirectResponse("/", status_code=303)
    with Session(engine) as s:
        columns = _get_columns(s) if (user.has_permission("view_current_columns")
                                       or user.has_permission("manage_columns")) else []
        products = s.exec(select(Product).order_by(Product.name)).all() if user.has_permission("manage_products") else []
        store_settings = s.exec(select(StoreSettings)).first()
        return_deduction_percent = store_settings.return_deduction_percent if store_settings else 15
        product_creators = {}
        for p in products:
            if p.created_by_id:
                u = s.get(User, p.created_by_id)
                if u:
                    product_creators[p.id] = u.username
    return render(request, "admin.html", columns=columns, products=products,
                  product_creators=product_creators,
                  return_deduction_percent=return_deduction_percent,
                  can_view_purchase_cost=user.has_permission("view_purchase_cost"))


# Reports

@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, user: User = Depends(require_permission("view_reports"))):
    return render(request, "reports.html")


@app.get("/api/reports/data")
def reports_data(request: Request, user: User = Depends(require_permission("view_reports"))):
    with Session(engine) as s:
        total_sales = s.exec(select(func.count(Sale.id))).one()
        total_items = s.exec(select(func.count(SaleItem.id))).one() or 0

        unit_discount_sum = s.exec(
            select(func.coalesce(func.sum(SaleItem.discount_per_unit * SaleItem.quantity), 0))
        ).one() or 0
        line_discount_sum = s.exec(
            select(func.coalesce(func.sum(SaleItem.discount_total), 0))
        ).one() or 0
        invoice_discount_sum = s.exec(
            select(func.coalesce(func.sum(Sale.discount_amount), 0))
        ).one() or 0
        discount_sum = int(unit_discount_sum) + int(line_discount_sum) + int(invoice_discount_sum)
        line_revenue = s.exec(
            select(func.coalesce(func.sum(SaleItem.final_unit_price * SaleItem.quantity - SaleItem.discount_total), 0))
        ).one() or 0
        revenue = max(0, int(line_revenue) - int(invoice_discount_sum))

        # top products by qty
        # Registered products are grouped by theier stable database identity
        # while ad-hoc invoice lines remain grouped by theier saved text name
        product_label = func.coalesce(Product.name, SaleItem.product_name)
        rows = s.exec(
            select(product_label, func.sum(SaleItem.quantity))
            .select_from(SaleItem)
            .outerjoin(Product, Product.id == SaleItem.product_id)
            .group_by(SaleItem.product_id, product_label)
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
    item_discount_total = sum((it.discount_per_unit * it.quantity) + int(getattr(it, "discount_total", 0) or 0) for it in items)
    total_final = sum(
        int(it.final_line_total) if getattr(it, "final_line_total", None) is not None
        else (it.final_unit_price * it.quantity - int(getattr(it, "discount_total", 0) or 0) - int(getattr(it, "invoice_discount_share", 0) or 0))
        for it in items
    )
    invoice_discount = int(getattr(sale, "discount_amount", 0) or 0)
    total_discount = item_discount_total + invoice_discount

    return {
        "id": sale.id,
        "created_at": sale.created_at,
        # Keep both names: templates may use values; APIs use items
        "items": items,
        "values": items,
        "sale_values": sale_values,
        "creator_username": creator.username if creator else None,
        "total_qty": total_qty,
        "total_final": total_final,
        "total_discount": total_discount,
        "invoice_discount": invoice_discount,
    }


@app.get("/sales", response_class=HTMLResponse)
def sales_page(request: Request, user: User = Depends(require_permission("view_sales"))):
    with Session(engine) as s:
        sale_columns = _get_columns(s, scope="sale")
        sales = s.exec(select(Sale).order_by(desc(Sale.id)).limit(100)).all()
        enriched = [_enrich_sale(s, sale) for sale in sales]
    return render(request, "sales.html", sales=enriched, sale_columns=sale_columns)


@app.get("/returns", response_class=HTMLResponse)
def returns_index(request: Request, user: User = Depends(require_permission("view_sales"))):
    with Session(engine) as s:
        sales = s.exec(select(Sale).order_by(desc(Sale.id)).limit(100)).all()
        rows = []
        for sale in sales:
            items = s.exec(select(SaleItem).where(SaleItem.sale_id == sale.id)).all()
            returned_total = 0
            for item in items:
                val = s.exec(select(func.coalesce(func.sum(SaleReturnItem.quantity), 0))
                             .where(SaleReturnItem.sale_item_id == item.id)).one()
                returned_total += int(val or 0)
            rows.append({
                "id": sale.id,
                "created_at": sale.created_at,
                "items_summary": "، ".join(f"{it.quantity}× {it.product_name}" for it in items),
                "total": max(0, sum(
                    int(it.final_line_total) if getattr(it, "final_line_total", None) is not None
                    else it.final_unit_price * it.quantity - int(getattr(it, "discount_total", 0) or 0) - int(getattr(it, "invoice_discount_share", 0) or 0)
                    for it in items
                )),
                "returned_quantity": returned_total,
            })
    return render(request, "returns.html", sales=rows)


@app.get("/sales/{sale_id}/returns", response_class=HTMLResponse)
def sale_returns_page(sale_id: int, request: Request,
                      user: User = Depends(require_permission("view_sales"))):
    with Session(engine) as s:
        sale = s.get(Sale, sale_id)
        if not sale:
            raise HTTPException(404, "فاکتور پیدا نشد")
        settings = s.exec(select(StoreSettings)).first()
        deduction_percent = max(0, min(100, int(settings.return_deduction_percent or 0))) if settings else 15
        items = s.exec(select(SaleItem).where(SaleItem.sale_id == sale_id).order_by(SaleItem.id)).all()
        item_rows = []
        item_by_id = {it.id: it for it in items}
        for item in items:
            returned = s.exec(select(func.coalesce(func.sum(SaleReturnItem.quantity), 0))
                              .where(SaleReturnItem.sale_item_id == item.id)).one()
            returned_qty = int(returned or 0)
            product = s.get(Product, item.product_id) if item.product_id else None
            payable_total = (int(item.final_line_total) if getattr(item, "final_line_total", None) is not None
                             else item.final_unit_price * item.quantity - int(getattr(item, "discount_total", 0) or 0)
                                  - int(getattr(item, "invoice_discount_share", 0) or 0))
            item_rows.append({
                "item": item,
                "returned_qty": returned_qty,
                "remaining_qty": max(0, item.quantity - returned_qty),
                "payable_total": max(0, payable_total),
                "can_restock": bool(product),
            })

        return_rows = []
        previous_returns = s.exec(select(SaleReturn).where(SaleReturn.sale_id == sale_id)
                                  .order_by(desc(SaleReturn.created_at))).all()
        for ret in previous_returns:
            actor = s.get(User, ret.created_by_id) if ret.created_by_id else None
            details = []
            ret_items = s.exec(select(SaleReturnItem).where(SaleReturnItem.return_id == ret.id)).all()
            for ritem in ret_items:
                sale_item = item_by_id.get(ritem.sale_item_id) or s.get(SaleItem, ritem.sale_item_id)
                details.append({
                    "name": sale_item.product_name if sale_item else "قلم حذف‌شده",
                    "quantity": ritem.quantity,
                    "restock_quantity": ritem.restock_quantity,
                    "refund_amount": ritem.refund_amount,
                    "deduction_percent": ritem.deduction_percent,
                })
            return_rows.append({
                "id": ret.id,
                "created_at": ret.created_at,
                "total_refund": ret.total_refund,
                "note": ret.note,
                "actor": actor.username if actor else "—",
                "items": details,
            })
    return render(request, "sale_returns.html", sale=sale, item_rows=item_rows,
                  return_rows=return_rows, deduction_percent=deduction_percent)


@app.post("/api/sales/{sale_id}/returns")
async def create_sale_return(sale_id: int, request: Request,
                             user: User = Depends(require_permission("view_sales"))):
    verify_csrf_header(request)
    body = await request.json()
    if not body.get("accepted_by_seller"):
        raise HTTPException(400, "ثبت مرجوعی باید توسط فروشنده تأیید شود")
    raw_items = body.get("items") or []
    if not raw_items:
        raise HTTPException(400, "حداقل یک قلم برای مرجوعی انتخاب کنید")

    with Session(engine) as s:
        sale = s.get(Sale, sale_id)
        if not sale:
            raise HTTPException(404, "فاکتور پیدا نشد")
        settings = s.exec(select(StoreSettings)).first()
        deduction_percent = max(0, min(100, int(settings.return_deduction_percent or 0))) if settings else 15

        prepared = []
        seen_item_ids = set()
        for raw in raw_items:
            try:
                sale_item_id = int(raw.get("sale_item_id"))
                qty = int(raw.get("quantity", 0))
                restock_qty = int(raw.get("restock_quantity", 0))
                refund_amount = int(raw.get("refund_amount", 0))
            except (ValueError, TypeError):
                raise HTTPException(400, "اطلاعات یکی از اقلام مرجوعی نامعتبر است")
            if sale_item_id in seen_item_ids:
                raise HTTPException(400, "یک قلم در درخواست مرجوعی تکرار شده است")
            seen_item_ids.add(sale_item_id)
            if qty < 1 or restock_qty < 0 or restock_qty > qty or refund_amount < 0:
                raise HTTPException(400, "تعداد یا مبلغ مرجوعی نامعتبر است")

            item = s.get(SaleItem, sale_item_id)
            if not item or item.sale_id != sale_id:
                raise HTTPException(400, "قلم انتخاب‌شده متعلق به این فاکتور نیست")
            already_returned = s.exec(select(func.coalesce(func.sum(SaleReturnItem.quantity), 0))
                                      .where(SaleReturnItem.sale_item_id == sale_item_id)).one()
            remaining_qty = item.quantity - int(already_returned or 0)
            if qty > remaining_qty:
                raise HTTPException(400, f"از «{item.product_name}» فقط {max(0, remaining_qty)} عدد باقی مانده که مرجوع نشده است")

            product = s.get(Product, item.product_id) if item.product_id else None
            if restock_qty and not product:
                raise HTTPException(400, f"برای «{item.product_name}» محصول انباری متناظر وجود ندارد")

            line_payable = (int(item.final_line_total) if getattr(item, "final_line_total", None) is not None
                            else int(item.final_unit_price or 0) * item.quantity
                                 - int(getattr(item, "discount_total", 0) or 0)
                                 - int(getattr(item, "invoice_discount_share", 0) or 0))
            already_qty = int(already_returned or 0)
            before_amount = (max(0, line_payable) * already_qty) // max(1, item.quantity)
            after_amount = (max(0, line_payable) * (already_qty + qty)) // max(1, item.quantity)
            returned_basis_amount = max(0, after_amount - before_amount)
            suggested_refund = (returned_basis_amount * (100 - deduction_percent) + 50) // 100
            prepared.append({
                "item": item,
                "product": product,
                "quantity": qty,
                "restock_quantity": restock_qty,
                "suggested_refund_amount": suggested_refund,
                "refund_amount": refund_amount,
            })

        note = str(body.get("note", "")).strip()[:1000] or None
        return_record = SaleReturn(
            sale_id=sale_id,
            total_refund=sum(row["refund_amount"] for row in prepared),
            note=note,
            accepted_by_seller=True,
            created_by_id=user.id,
        )
        s.add(return_record)
        s.flush()

        for row in prepared:
            item = row["item"]
            s.add(SaleReturnItem(
                return_id=return_record.id,
                sale_item_id=item.id,
                quantity=row["quantity"],
                restock_quantity=row["restock_quantity"],
                deduction_percent=deduction_percent,
                suggested_refund_amount=row["suggested_refund_amount"],
                refund_amount=row["refund_amount"],
            ))
            if row["restock_quantity"]:
                product = row["product"]
                s.exec(
                    sa_update(Product).where(Product.id == product.id).values(
                        stock_quantity=Product.stock_quantity + row["restock_quantity"],
                        updated_at=utcnow(), updated_by_id=user.id,
                    ).execution_options(synchronize_session=False)
                )
                s.refresh(product)
                s.add(InventoryMovement(
                    product_id=product.id,
                    quantity_change=row["restock_quantity"],
                    reason="sale_return", reference_type="sale_return",
                    reference_id=return_record.id,
                    note=f"برگشت {row['restock_quantity']} عدد از فاکتور {sale_id}",
                    created_by_id=user.id,
                ))
        s.commit()
        return_id = return_record.id
        total_refund = return_record.total_refund

    write_audit(engine, request, "sale.return", user=user, target_type="sale", target_id=sale_id,
                metadata_json=json.dumps({"return_id": return_id, "total_refund": total_refund,
                                          "items_count": len(prepared)}, ensure_ascii=False))
    return {"ok": True, "return_id": return_id, "total_refund": total_refund,
            "redirect": f"/sales/{sale_id}/returns"}


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

        total_qty = sum(it.quantity for it in items)
        total_final = sum(
            int(it.final_line_total) if getattr(it, "final_line_total", None) is not None
            else it.final_unit_price * it.quantity - int(getattr(it, "discount_total", 0) or 0) - int(getattr(it, "invoice_discount_share", 0) or 0)
            for it in items
        )
        total_discount = sum(it.discount_per_unit * it.quantity + int(getattr(it, "discount_total", 0) or 0) for it in items) + int(getattr(sale, "discount_amount", 0) or 0)
        total_words = number_to_persian(total_final) + " تومان" if total_final else ""

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
                  "قیمت دیتابیس", "قیمت فروش", "تخفیف هر واحد", "تخفیف کل ردیف",
                  "تخفیف کل فاکتور (فقط ردیف اول)", "مبلغ نهایی ردیف"]
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
                    int(getattr(it, "discount_total", 0) or 0),
                    int(getattr(sale, "discount_amount", 0) or 0) if it == items[0] else 0,
                    int(it.final_line_total) if getattr(it, "final_line_total", None) is not None else it.final_unit_price * it.quantity - int(getattr(it, "discount_total", 0) or 0) - int(getattr(it, "invoice_discount_share", 0) or 0),
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
                elif op == "IN":
                    values = _parse_in_values(f.get("value", ""))
                    if field == "product_name":
                        vstmt = vstmt.where(field_obj.in_(values))
                    else:
                        numeric_values = [_to_int(item) for item in values]
                        numeric_values = [item for item in numeric_values if item is not None]
                        vstmt = vstmt.where(field_obj.in_(numeric_values))
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
                elif op == "IN":
                    values = _parse_in_values(f.get("value", ""))
                    vstmt = vstmt.where(SaleValue.value.in_(values))
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
        stmt = select(Product).where(Product.is_active == True)  # type: ignore
        if q:
            stmt = stmt.where(Product.name.like(f"%{_escape_like(q)}%", escape="\\"))
        products = s.exec(stmt.order_by(Product.name).limit(30)).all()
        return [{"id": p.id, "name": p.name, "price": p.price,
                 "stock_quantity": p.stock_quantity} for p in products]


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
        {"id": "item:discount_total", "name": "تخفیف کل ردیف", "type": "number", "options": None, "scope": "item"},
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
    try:
        invoice_discount_amount = int(body.get("invoice_discount_amount", 0) or 0)
    except (ValueError, TypeError):
        raise HTTPException(400, "تخفیف کل فاکتور نامعتبر است")
    if invoice_discount_amount < 0:
        raise HTTPException(400, "تخفیف کل فاکتور نمی‌تواند منفی باشد")
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
    stock_warnings = []
    with Session(engine) as s:
        all_columns = _get_columns(s)
        sale_cols = {c.id: c for c in all_columns if c.scope == "sale"}
        item_cols = {c.id: c for c in all_columns if c.scope == "item"}

        for c in sale_cols.values():
            if c.required and not str(sale_values_in.get(str(c.id), "")).strip():
                raise HTTPException(400, f"فیلد «{c.name}» الزامی است")

        products_by_id = {}
        quantities_by_product = {}
        for raw in raw_items:
            try:
                pid = raw.get("product_id")
                pid = int(pid) if pid else None
                qty = int(raw.get("quantity", 1))
                manual = int(raw.get("manual_unit_price", 0))
                discount = int(raw.get("discount_per_unit", 0))
                line_discount = int(raw.get("discount_total", 0) or 0)
            except (ValueError, TypeError):
                raise HTTPException(400, "اطلاعات یکی از اقلام نامعتبر است")

            if qty < 1:
                raise HTTPException(400, "تعداد باید حداقل ۱ باشد")
            if manual < 0 or discount < 0 or line_discount < 0:
                raise HTTPException(400, "قیمت و تخفیف نمی‌توانند منفی باشند")
            if discount > manual:
                raise HTTPException(400, "تخفیف هر واحد بیشتر از قیمت است")
            line_base_before_global = (manual - discount) * qty
            if line_discount > line_base_before_global:
                raise HTTPException(400, "تخفیف کل ردیف بیشتر از مبلغ همان ردیف است")

            product = None
            db_price = None
            purchase_price = None
            pname = ""
            if pid:
                product = products_by_id.get(pid) or s.get(Product, pid)
                if not product or not product.is_active:
                    raise HTTPException(400, f"محصول #{pid} یافت نشد یا غیرفعال است")
                products_by_id[pid] = product
                db_price = product.price
                purchase_price = product.purchase_price
                pname = product.name
                quantities_by_product[pid] = quantities_by_product.get(pid, 0) + qty
            else:
                # Ad-hoc items are intentionally allowed and do not affect inventory
                pname = str(raw.get("product_name", "")).strip()
                if not pname:
                    raise HTTPException(400, "نام کالا نمی‌تواند خالی باشد")

            final = manual - discount
            custom = {}
            for cid_str, value in (raw.get("custom_values") or {}).items():
                try:
                    cid = int(cid_str)
                except (ValueError, TypeError):
                    continue
                if cid in item_cols and str(value).strip():
                    custom[cid] = str(value).strip()
            for cid, col in item_cols.items():
                if col.required and not custom.get(cid):
                    raise HTTPException(400, f"فیلد «{col.name}» برای «{pname}» الزامی است")

            validated.append({
                "product_id": pid,
                "product_name": pname,
                "quantity": qty,
                "db_unit_price": db_price,
                "purchase_unit_price": purchase_price,
                "inventory_deducted": bool(product),
                "manual_unit_price": manual,
                "discount_per_unit": discount,
                "discount_total": line_discount,
                "final_unit_price": final,
                "custom_values": custom,
            })


        line_amounts = [max(0, v["final_unit_price"] * v["quantity"] - v["discount_total"]) for v in validated]
        subtotal_after_item_discounts = sum(line_amounts)
        if invoice_discount_amount > subtotal_after_item_discounts:
            raise HTTPException(400, "تخفیف کل فاکتور بیشتر از مبلغ سبد خرید است")
        allocations = [0] * len(validated)
        if invoice_discount_amount and subtotal_after_item_discounts:
            remainders = []
            for index, line_amount in enumerate(line_amounts):
                numerator = invoice_discount_amount * line_amount
                allocations[index] = numerator // subtotal_after_item_discounts
                remainders.append((numerator % subtotal_after_item_discounts, index))
            leftover = invoice_discount_amount - sum(allocations)
            for _, index in sorted(remainders, reverse=True)[:leftover]:
                allocations[index] += 1
        for index, values in enumerate(validated):
            values["invoice_discount_share"] = allocations[index]
            values["final_line_total"] = max(0, line_amounts[index] - allocations[index])

        # The invoice and stock updates are committed as one transaction
        sale = Sale(created_at=created_at, created_by_id=user.id,
                    discount_amount=invoice_discount_amount)
        s.add(sale)
        s.flush()
        sale_id = sale.id

        for values in validated:
            custom = values.pop("custom_values", {})
            item = SaleItem(sale_id=sale_id, **values)
            s.add(item)
            s.flush()
            for cid, value in custom.items():
                s.add(SaleItemValue(item_id=item.id, column_id=cid, value=value))

        for pid, sold_qty in quantities_by_product.items():
            product = products_by_id[pid]
            # Use an atomic SQL arithmetic update so simultaneous sales cannot overwrite
            # each other's stock deductions
            s.exec(
                sa_update(Product).where(Product.id == pid).values(
                    stock_quantity=Product.stock_quantity - sold_qty,
                    updated_at=utcnow(), updated_by_id=user.id,
                ).execution_options(synchronize_session=False)
            )
            s.refresh(product)
            new_stock = int(product.stock_quantity or 0)
            previous_stock = new_stock + sold_qty
            s.add(InventoryMovement(
                product_id=pid, quantity_change=-sold_qty,
                reason="sale", reference_type="sale", reference_id=sale_id,
                note=f"فروش فاکتور شماره {sale_id}", created_by_id=user.id,
            ))
            if new_stock < 0:
                stock_warnings.append({
                    "product_name": product.name,
                    "previous_stock": previous_stock,
                    "sold_quantity": sold_qty,
                    "new_stock": new_stock,
                })

        for cid_str, value in sale_values_in.items():
            try:
                cid = int(cid_str)
            except (ValueError, TypeError):
                continue
            text_value = str(value).strip()
            if cid in sale_cols and text_value:
                s.add(SaleValue(sale_id=sale_id, column_id=cid, value=text_value))
        s.commit()

    total_discount = (sum(v["discount_per_unit"] * v["quantity"] + v["discount_total"] for v in validated)
                      + invoice_discount_amount)
    write_audit(engine, request, "sale.create_invoice", user=user,
                target_type="sale", target_id=sale_id,
                metadata_json=json.dumps({
                    "items_count": len(validated),
                    "total_discount": total_discount,
                    "stock_warnings": stock_warnings,
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
        "stock_warnings": stock_warnings,
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
        if s.exec(select(SaleReturn).where(SaleReturn.sale_id == sid)).first():
            raise HTTPException(400, "برای فاکتوری که مرجوعی ثبت‌شده دارد، حذف انجام نمی‌شود")

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
                "discount_amount": int(getattr(sale, "discount_amount", 0) or 0),
            },
            "items": [
                {
                    "old_item_id": it.id,
                    "product_id": it.product_id,
                    "product_name": it.product_name,
                    "quantity": it.quantity,
                    "db_unit_price": it.db_unit_price,
                    "purchase_unit_price": it.purchase_unit_price,
                    "inventory_deducted": bool(it.inventory_deducted),
                    "manual_unit_price": it.manual_unit_price,
                    "discount_per_unit": it.discount_per_unit,
                    "discount_total": int(getattr(it, "discount_total", 0) or 0),
                    "invoice_discount_share": int(getattr(it, "invoice_discount_share", 0) or 0),
                    "final_line_total": getattr(it, "final_line_total", None),
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

        # Put back stock only for sales made after inventory tracking was enabled
        for it in items:
            if it.inventory_deducted and it.product_id:
                product = s.get(Product, it.product_id)
                if product:
                    s.exec(
                        sa_update(Product).where(Product.id == product.id).values(
                            stock_quantity=Product.stock_quantity + it.quantity,
                            updated_at=utcnow(), updated_by_id=user.id,
                        ).execution_options(synchronize_session=False)
                    )
                    s.refresh(product)
                    s.add(InventoryMovement(
                        product_id=product.id, quantity_change=it.quantity,
                        reason="sale_deleted", reference_type="deleted_sale", reference_id=sid,
                        note=f"بازگشت موجودی بابت حذف فاکتور {sid}", created_by_id=user.id,
                    ))

        for iv in item_values:
            s.delete(iv)
        for it in items:
            s.delete(it)
        for value in vals:
            s.delete(value)
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
                elif op == "IN":
                    values = _parse_in_values(f.get("value", ""))
                    if field == "product_name":
                        vstmt = vstmt.where(field_obj.in_(values))
                    else:
                        numeric_values = [_to_int(item) for item in values]
                        numeric_values = [item for item in numeric_values if item is not None]
                        vstmt = vstmt.where(field_obj.in_(numeric_values))
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
                elif op == "IN":
                    values = _parse_in_values(f.get("value", ""))
                    vstmt = vstmt.where(SaleValue.value.in_(values))
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


def _parse_optional_amount(raw, field_label: str) -> int | None:
    text_value = str(raw or "").strip().replace(",", "").replace("٬", "")
    if not text_value:
        return None
    value = _to_int(text_value)
    if value is None or value < 0:
        raise HTTPException(400, f"{field_label} نامعتبر است")
    return value


def _parse_stock_value(raw, field_label: str = "موجودی") -> int:
    value = _to_int(str(raw or "0").strip().replace(",", "").replace("٬", ""))
    if value is None:
        raise HTTPException(400, f"{field_label} نامعتبر است")
    return value


@app.post("/api/product/add")
async def add_product(request: Request, user: User = Depends(require_permission("manage_products"))):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    name = str(form.get("name", "")).strip()
    price = _parse_optional_amount(form.get("price"), "قیمت فروش")
    purchase_price = (_parse_optional_amount(form.get("purchase_price"), "قیمت خرید")
                      if user.has_permission("view_purchase_cost") else None)
    initial_stock = _parse_stock_value(form.get("stock_quantity", "0"), "موجودی اولیه")
    if initial_stock < 0:
        raise HTTPException(400, "موجودی اولیه نمی‌تواند منفی باشد")
    if not name:
        raise HTTPException(400, "نام محصول الزامی است")

    with Session(engine) as s:
        if s.exec(select(Product).where(Product.name == name)).first():
            raise HTTPException(400, f"محصول «{name}» قبلاً وجود دارد")
        p = Product(name=name, price=price, purchase_price=purchase_price,
                    stock_quantity=initial_stock, is_active=True,
                    created_by_id=user.id, updated_by_id=user.id)
        s.add(p)
        s.flush()
        pid = p.id
        if initial_stock:
            s.add(InventoryMovement(
                product_id=pid, quantity_change=initial_stock,
                reason="initial_stock", note="موجودی اولیه هنگام ساخت محصول",
                created_by_id=user.id,
            ))
        s.commit()

    write_audit(engine, request, "product.create", user=user, target_type="product", target_id=pid,
                metadata_json=json.dumps({"name": name, "price": price,
                                          "initial_stock": initial_stock}, ensure_ascii=False))
    return RedirectResponse("/admin", status_code=303)


@app.post("/api/product/{pid}/update")
async def update_product(pid: int, request: Request, user: User = Depends(require_permission("manage_products"))):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    new_name = str(form.get("name", "")).strip()
    new_price = _parse_optional_amount(form.get("price"), "قیمت فروش")
    new_purchase_price = (_parse_optional_amount(form.get("purchase_price"), "قیمت خرید")
                         if user.has_permission("view_purchase_cost") else None)
    new_stock = _parse_stock_value(form.get("stock_quantity", "0"))
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
        old_purchase_price = p.purchase_price
        old_stock = p.stock_quantity
        price_changed = old_price != new_price
        purchase_changed = (user.has_permission("view_purchase_cost")
                            and old_purchase_price != new_purchase_price)
        stock_delta = new_stock - old_stock

        p.name = new_name
        p.price = new_price
        if user.has_permission("view_purchase_cost"):
            p.purchase_price = new_purchase_price
        p.stock_quantity = new_stock
        p.updated_at = utcnow()
        p.updated_by_id = user.id
        s.add(p)

        if price_changed or purchase_changed:
            s.add(ProductPriceHistory(
                product_id=pid,
                old_price=old_price,
                new_price=new_price,
                old_purchase_price=old_purchase_price,
                new_purchase_price=p.purchase_price,
                changed_by_id=user.id,
            ))
        if stock_delta:
            s.add(InventoryMovement(
                product_id=pid, quantity_change=stock_delta,
                reason="manual_adjustment", note="اصلاح دستی موجودی از صفحه مدیریت محصولات",
                created_by_id=user.id,
            ))
        s.commit()

    write_audit(engine, request, "product.update", user=user, target_type="product", target_id=pid,
                metadata_json=json.dumps({"stock_delta": stock_delta,
                                          "purchase_price_changed": purchase_changed}, ensure_ascii=False))
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
        if not p:
            raise HTTPException(404, "محصول پیدا نشد")
        # Historical invoice references must remain valid: archive instead of hard-delete
        p.is_active = False
        p.updated_at = utcnow()
        p.updated_by_id = user.id
        s.add(p)
        s.commit()
    write_audit(engine, request, "product.archive", user=user, target_type="product", target_id=pid)
    return RedirectResponse("/admin", status_code=303)


@app.post("/api/product/{pid}/activate")
async def activate_product(pid: int, request: Request, user: User = Depends(require_permission("manage_products"))):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    with Session(engine) as s:
        product = s.get(Product, pid)
        if not product:
            raise HTTPException(404, "محصول پیدا نشد")
        product.is_active = True
        product.updated_at = utcnow()
        product.updated_by_id = user.id
        s.add(product)
        s.commit()
    write_audit(engine, request, "product.activate", user=user, target_type="product", target_id=pid)
    return RedirectResponse("/admin", status_code=303)


@app.get("/api/product/{pid}/history")
def product_history(pid: int, user: User = Depends(require_user)):
    can_view_cost = user.has_permission("view_purchase_cost")
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
            item = {
                "old_price": r.old_price,
                "new_price": r.new_price,
                "changed_by": changer.username if changer else "?",
                "changed_at": r.changed_at.isoformat(),
            }
            if can_view_cost:
                item["old_purchase_price"] = r.old_purchase_price
                item["new_purchase_price"] = r.new_purchase_price
            result.append(item)
        return result


@app.get("/api/product/{pid}/stock-history")
def product_stock_history(pid: int, user: User = Depends(require_permission("manage_products"))):
    with Session(engine) as s:
        p = s.get(Product, pid)
        if not p:
            raise HTTPException(404, "محصول پیدا نشد")
        rows = s.exec(select(InventoryMovement)
                      .where(InventoryMovement.product_id == pid)
                      .order_by(desc(InventoryMovement.created_at))
                      .limit(100)).all()
        result = []
        for row in rows:
            actor = s.get(User, row.created_by_id) if row.created_by_id else None
            result.append({
                "quantity_change": row.quantity_change,
                "reason": row.reason,
                "note": row.note,
                "created_by": actor.username if actor else "سیستم",
                "created_at": row.created_at.isoformat(),
            })
        return result


@app.post("/api/owner/return-settings/update")
async def owner_return_settings_update(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    raw_percent = _to_int(form.get("return_deduction_percent"))
    if raw_percent is None or raw_percent < 0 or raw_percent > 100:
        raise HTTPException(400, "درصد کسر باید عددی بین ۰ تا ۱۰۰ باشد")
    with Session(engine) as s:
        settings = s.exec(select(StoreSettings)).first()
        if not settings:
            settings = StoreSettings(name="ShopCRM")
        settings.return_deduction_percent = raw_percent
        settings.updated_at = utcnow()
        s.add(settings)
        s.commit()
    write_audit(engine, request, "settings.return_deduction.update", user=user,
                metadata_json=json.dumps({"return_deduction_percent": raw_percent}))
    return RedirectResponse("/admin", status_code=303)


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
            can_view_purchase_cost=form.get("can_view_purchase_cost") == "on",
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
        u.can_view_purchase_cost = form.get("can_view_purchase_cost") == "on"
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
            except (ValueError, TypeError):
                pass

        sale = Sale(
            id=target_id,
            created_at=created_at,
            created_by_id=sale_info.get("created_by_id"),
            note=sale_info.get("note"),
            discount_amount=int(sale_info.get("discount_amount", 0) or 0),
        )
        s.add(sale)
        s.flush()

        restored_item_ids = {}
        for it in data.get("items", []):
            item = SaleItem(
                sale_id=sale.id,
                product_id=it.get("product_id"),
                product_name=it.get("product_name", "?"),
                quantity=int(it.get("quantity", 1)),
                db_unit_price=it.get("db_unit_price"),
                purchase_unit_price=it.get("purchase_unit_price"),
                inventory_deducted=bool(it.get("inventory_deducted", False)),
                manual_unit_price=int(it.get("manual_unit_price", 0)),
                discount_per_unit=int(it.get("discount_per_unit", 0)),
                discount_total=int(it.get("discount_total", 0) or 0),
                invoice_discount_share=int(it.get("invoice_discount_share", 0) or 0),
                final_line_total=(int(it["final_line_total"]) if it.get("final_line_total") is not None else None),
                final_unit_price=int(it.get("final_unit_price", 0)),
            )
            s.add(item)
            s.flush()
            if it.get("old_item_id") is not None:
                restored_item_ids[int(it["old_item_id"])] = item.id

            if item.inventory_deducted and item.product_id:
                product = s.get(Product, item.product_id)
                if product:
                    s.exec(
                        sa_update(Product).where(Product.id == product.id).values(
                            stock_quantity=Product.stock_quantity - item.quantity,
                            updated_at=utcnow(), updated_by_id=user.id,
                        ).execution_options(synchronize_session=False)
                    )
                    s.refresh(product)
                    s.add(InventoryMovement(
                        product_id=product.id, quantity_change=-item.quantity,
                        reason="sale_restored", reference_type="sale", reference_id=sale.id,
                        note=f"کسر مجدد موجودی بابت بازیابی فاکتور {sale.id}", created_by_id=user.id,
                    ))

        for iv in data.get("item_values", []):
            restored_item_id = restored_item_ids.get(int(iv.get("item_id", -1)))
            if restored_item_id:
                s.add(SaleItemValue(
                    item_id=restored_item_id,
                    column_id=iv["column_id"],
                    value=iv["value"],
                ))

        for value in data.get("values", []):
            s.add(SaleValue(
                sale_id=sale.id,
                column_id=value["column_id"],
                value=value["value"],
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
