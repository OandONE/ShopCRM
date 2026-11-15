from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Session, create_engine, select, func
from sqlalchemy import Float, cast, or_, and_
from models import Admin, AdminSession, Column, Product, Sale, SaleValue
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from collections import defaultdict
import uvicorn, hashlib, hmac, secrets, io, csv
from datetime import datetime, timedelta, timezone
import io, csv
from fastapi.responses import Response
from sqlalchemy import cast, Float
from datetime import timedelta

# Setup
engine = create_engine("sqlite:///database.db", connect_args={"check_same_thread": False})


# Auth helpers (PBKDF2 + legacy SHA256 fallback)
def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def verify_password(password: str, stored: str) -> bool:
    """support old hash sha256"""
    if stored.startswith("pbkdf2$"):
        _, salt_hex, digest_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return hmac.compare_digest(expected.hex(), digest_hex)
    # legacy
    return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored)


SESSION_COOKIE = "shopcrm_session"
SESSION_DAYS = 7


def create_session(admin_id: int) -> AdminSession:
    return AdminSession(
        token=secrets.token_urlsafe(32),
        admin_id=admin_id,
        csrf_token=secrets.token_urlsafe(24),
        expires_at=utcnow() + timedelta(days=SESSION_DAYS),
    )


def get_session(request: Request) -> AdminSession | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with Session(engine) as s:
        sess = s.exec(
            select(AdminSession).where(
                AdminSession.token == token,
                AdminSession.expires_at > utcnow(),
            )
        ).first()
        return sess


def check_admin(request: Request) -> AdminSession:
    sess = get_session(request)
    if not sess:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    request.state.admin_session = sess
    return sess


def verify_csrf(request: Request, form_token: str) -> None:
    sess = get_session(request)
    if not sess or not form_token or not hmac.compare_digest(sess.csrf_token, form_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


# ---------- Login rate limiting (in-memory) ----------
_login_attempts: dict[str, list[datetime]] = {}


def _rate_ok(ip: str) -> bool:
    now = utcnow()
    window = timedelta(minutes=15)
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < window]
    _login_attempts[ip] = attempts
    return len(attempts) < 5


def _rate_hit(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(utcnow())


# ---------- Lifespan / migrations ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        # clear invalid sessions
        for s in session.exec(select(AdminSession).where(AdminSession.expires_at < utcnow())).all():
            session.delete(s)

        # defualt admin (if not admin exists)
        if not session.exec(select(Admin)).first():
            session.add(Admin(username="admin", password_hash=hash_password("admin")))

        # defualt cloumn
        if not session.exec(select(Column)).first():
            # migrane: des cloumn -> textarea
            desc_col = session.exec(select(Column).where(Column.name == "توضیحات")).first()
            if desc_col and desc_col.type == "text":
                desc_col.type = "textarea"
                session.add(desc_col)
            defaults = [
                Column(name="محصول", type="product", required=True, is_default=True, order=0),
                Column(name="قیمت (تومان)", type="number", required=True, is_default=True, order=1),
                Column(name="تعداد", type="number", required=True, is_default=True, order=2),
                Column(name="جنسیت خریدار", type="choice", options="مرد,زن", is_default=True, order=3),
                Column(name="سن حدودی خریدار", type="number", is_default=False, order=4),
                Column(name="قد حدودی (سانت)", type="number", is_default=False, order=5),
                Column(name="ساعت خرید", type="text", is_default=False, order=6),
                Column(name="توضیحات", type="text", is_default=False, order=7),
            ]
            for col in defaults:
                session.add(col)

        # defualt products
        if not session.exec(select(Product)).first():
            for p in ["پنیر", "شیر", "ماست", "دوغ", "کره", "خامه", "عسل", "نان", "برنج", "روغن"]:
                session.add(Product(name=p))

        session.commit()

        existing_ids = set(session.exec(select(SaleValue.sale_id)).all())
        known_ids = {s.id for s in session.exec(select(Sale)).all()}
        for sid in existing_ids - known_ids:
            session.add(Sale(id=sid, created_at=utcnow()))
        session.commit()

    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Number to Persian (باگ ۱۱-۱۹ فیکس شد) ----------
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


# Pages
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login(request: Request):
    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(ip):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "تعداد تلاش زیاد. ۱۵ دقیقه صبر کن."},
            status_code=429,
        )
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    with Session(engine) as s:
        admin = s.exec(select(Admin).where(Admin.username == username)).first()
        if admin and verify_password(password, admin.password_hash):
            # migrane old hash
            if not admin.password_hash.startswith("pbkdf2$"):
                admin.password_hash = hash_password(password)
            sess = create_session(admin.id or 0)
            s.add(sess)
            s.add(admin)
            s.commit()
            resp = RedirectResponse("/admin", status_code=303)
            resp.set_cookie(
                SESSION_COOKIE, sess.token,
                httponly=True, samesite="lax", secure=False,
                max_age=SESSION_DAYS * 24 * 3600,
            )
            return resp
    _rate_hit(ip)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "نام کاربری یا رمز اشتباه"},
        status_code=401,
    )


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with Session(engine) as s:
            sess = s.exec(select(AdminSession).where(AdminSession.token == token)).first()
            if sess:
                s.delete(sess)
                s.commit()
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def _get_columns(session, default_only=False):
    q = select(Column).order_by(Column.order.asc())
    if default_only:
        q = q.where(Column.is_default == True)  # type: ignore
    return session.exec(q).all()


@app.get("/quick", response_class=HTMLResponse)
def quick_form(request: Request):
    with Session(engine) as s:
        columns = _get_columns(s, default_only=True)
        products = s.exec(select(Product).order_by(Product.name)).all()
    return templates.TemplateResponse("quick.html", {"request": request, "columns": columns, "products": products})


@app.get("/full", response_class=HTMLResponse)
def full_form(request: Request):
    with Session(engine) as s:
        columns = _get_columns(s)
        products = s.exec(select(Product).order_by(Product.name)).all()
    return templates.TemplateResponse("full.html", {"request": request, "columns": columns, "products": products})


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request):
    return templates.TemplateResponse("search.html", {"request": request})


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, sess: AdminSession = Depends(check_admin)):
    with Session(engine) as s:
        columns = _get_columns(s)
        products = s.exec(select(Product).order_by(Product.name)).all()
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "columns": columns, "products": products, "csrf_token": sess.csrf_token},
    )


@app.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request, sess: AdminSession = Depends(check_admin)):
    return templates.TemplateResponse("backup.html", {"request": request, "csrf_token": sess.csrf_token})


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, _: AdminSession = Depends(check_admin)):
    return templates.TemplateResponse("reports.html", {"request": request})


@app.get("/sales", response_class=HTMLResponse)
def sales_page(request: Request, sess: AdminSession = Depends(check_admin)):
    with Session(engine) as s:
        columns = _get_columns(s)
        sales = s.exec(select(Sale).order_by(Sale.id.desc()).limit(200)).all()
        sales_data = []
        for sale in sales:
            col_values = {v.column_id: v.value for v in s.exec(
                select(SaleValue).where(SaleValue.sale_id == sale.id)
            ).all()}
            sales_data.append({
                "id": sale.id,
                "created_at": sale.created_at,
                "data": col_values,
            })
    return templates.TemplateResponse(
        "sales.html",
        {"request": request, "sales": sales_data, "columns": columns, "csrf_token": sess.csrf_token},
    )


# API: number / price
@app.get("/api/price-to-words")
def price_to_words(price: str = ""):
    try:
        p = int(price)
    except (ValueError, TypeError):
        return {"words": ""}
    return {"words": number_to_persian(p) + " تومان"}


# API: save sale
ALLOWED_SOURCES = {"quick", "full"}

@app.post("/api/sale")
async def save_sale(request: Request):
    form = await request.form()

    raw_source = str(form.get("source", "") or "").strip()
    source = raw_source if raw_source in ALLOWED_SOURCES else "quick"

    with Session(engine) as s:
        columns = _get_columns(s)
        # required_ids

        values: dict[int, str] = {}
        for key, raw in form.items():
            if not key.startswith("col_"):
                continue
            try:
                cid = int(key.replace("col_", "", 1))
            except ValueError:
                continue
            val = str(raw).strip()
            if val:
                values[cid] = val

        missing = [c.name for c in columns if c.required and not values.get(c.id)]
        if missing:
            raise HTTPException(status_code=400, detail="فیلدهای اجباری خالی: " + "، ".join(missing))

        sale = Sale()
        s.add(sale)
        s.commit()
        s.refresh(sale)
        for cid, val in values.items():
            s.add(SaleValue(sale_id=sale.id, column_id=cid, value=val))
        s.commit()

    return RedirectResponse(f"/{source}", status_code=303)


# API: products
def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


@app.get("/api/search-products")
def search_products(q: str = ""):
    with Session(engine) as s:
        if q:
            stmt = select(Product).where(Product.name.like(f"%{_escape_like(q)}%", escape="\\"))
            products = s.exec(stmt).all()
        else:
            products = s.exec(select(Product).order_by(Product.name)).all()
        return [{"id": p.id, "name": p.name} for p in products]


# API: columns list
@app.get("/api/columns")
def list_columns():
    with Session(engine) as s:
        cols = _get_columns(s)
        return [{"id": c.id, "name": c.name, "type": c.type, "options": c.options} for c in cols]


# API: advanced search
@app.post("/api/search-advanced")
async def search_advanced(request: Request):
    body = await request.json()
    filters = body.get("filters", [])
    with Session(engine) as s:
        all_sales = s.exec(select(Sale.id)).all()
        result: set[int] | None = None

        for i, f in enumerate(filters):
            try:
                cid = int(f["colId"])
            except (KeyError, ValueError, TypeError):
                continue
            op = f.get("op", "=")
            val = str(f.get("value", ""))
            logic = (f.get("logic") or "AND").upper() if i > 0 else "AND"

            stmt = select(SaleValue.sale_id).where(SaleValue.column_id == cid)
            if op == "=":
                stmt = stmt.where(SaleValue.value == val)
            elif op == "like":
                stmt = stmt.where(SaleValue.value.like(f"%{_escape_like(val)}%", escape="\\"))
            elif op in (">", "<", ">=", "<="):
                try:
                    num = float(val)
                except ValueError:
                    continue
                col_as_num = cast(SaleValue.value, Float)
                stmt = stmt.where({
                    ">": col_as_num > num, "<": col_as_num < num,
                    ">=": col_as_num >= num, "<=": col_as_num <= num,
                }[op])

            ids = set(s.exec(stmt).all())
            if result is None:
                result = ids
            elif logic == "OR":
                result |= ids
            elif logic == "NOT":
                result -= ids
            else:  # AND
                result &= ids

            if result == set():
                break

        if result is None:
            result = set(all_sales)

        # out
        columns = {c.id: c.name for c in _get_columns(s)}
        sales_out = []
        for sid in sorted(result, reverse=True)[:100]:
            values = s.exec(select(SaleValue).where(SaleValue.sale_id == sid)).all()
            data = {columns.get(v.column_id, f"ستون {v.column_id}"): v.value for v in values}
            sales_out.append({"sale_id": sid, "data": data})
        return sales_out

from datetime import timedelta

def _parse_iso_date(s: str):
    """YYYY-MM-DD -> datetime naive UTC"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def _query_sale_ids(body: dict) -> list[int]:
    """filter /api/sales/query and /api/sales/export.csv"""
    from_date = _parse_iso_date(body.get("from_date", ""))
    to_date = _parse_iso_date(body.get("to_date", ""))
    filters = body.get("filters") or []

    with Session(engine) as s:
        stmt = select(Sale.id)
        if from_date:
            stmt = stmt.where(Sale.created_at >= from_date)
        if to_date:
            # inclusive end-of-day
            stmt = stmt.where(Sale.created_at < to_date + timedelta(days=1))
        result: set[int] = set(s.exec(stmt).all())

        for i, f in enumerate(filters):
            try:
                cid = int(f["colId"])
            except (KeyError, ValueError, TypeError):
                continue
            op = f.get("op", "=")
            val = str(f.get("value", ""))
            logic = (f.get("logic") or "AND").upper() if i > 0 else "AND"

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


@app.post("/api/sales/query")
async def sales_query(request: Request, _: AdminSession = Depends(check_admin)):
    body = await request.json()
    ids = _query_sale_ids(body)
    with Session(engine) as s:
        columns = _get_columns(s)
        name_by_id = {c.id: c.name for c in columns}
        result = []
        for sid in ids[:500]:
            sale = s.get(Sale, sid)
            if not sale:
                continue
            vals = s.exec(select(SaleValue).where(SaleValue.sale_id == sid)).all()
            data = {str(v.column_id): v.value for v in vals}
            result.append({
                "sale_id": sid,
                "created_at": sale.created_at.isoformat(),
                "data": data,
            })
    return result


@app.post("/api/sales/export.csv")
async def sales_export(request: Request, _: AdminSession = Depends(check_admin)):
    body = await request.json()
    ids = _query_sale_ids(body)
    with Session(engine) as s:
        columns = _get_columns(s)
        buf = io.StringIO()
        buf.write("\ufeff")  # BOM for Excel persian
        writer = csv.writer(buf)
        writer.writerow(["شماره", "تاریخ"] + [c.name for c in columns])
        for sid in ids:
            sale = s.get(Sale, sid)
            if not sale:
                continue
            vals = {v.column_id: v.value for v in s.exec(
                select(SaleValue).where(SaleValue.sale_id == sid)
            ).all()}
            row = [sid, sale.created_at.strftime("%Y-%m-%d %H:%M")]
            for c in columns:
                row.append(vals.get(c.id, ""))
            writer.writerow(row)

    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sales.csv"'},
    )

# API: admin actions (column/product CRUD by CSRF)
@app.post("/api/column/add")
async def add_column(request: Request, _: AdminSession = Depends(check_admin)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    with Session(engine) as s:
        col = Column(
            name=str(form.get("name", "ستون جدید")).strip() or "ستون جدید",
            type=str(form.get("type", "text")),
            options=str(form.get("options")) or None,
            required=form.get("required") == "on",
            is_default=form.get("is_default") == "on",
            order=len(s.exec(select(Column)).all()),
        )
        s.add(col)
        s.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/api/column/delete")
async def delete_column(request: Request, _: AdminSession = Depends(check_admin)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    try:
        cid = int(str(form.get("id")))
    except (ValueError, TypeError):
        raise HTTPException(400, "شناسه ستون نامعتبره")
    with Session(engine) as s:
        col = s.get(Column, cid)
        if col:
            for v in s.exec(select(SaleValue).where(SaleValue.column_id == cid)).all():
                s.delete(v)
            s.delete(col)
            s.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/api/product/add")
async def add_product(request: Request, _: AdminSession = Depends(check_admin)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    name = str(form.get("name", "")).strip()
    if name:
        with Session(engine) as s:
            if not s.exec(select(Product).where(Product.name == name)).first():
                s.add(Product(name=name))
                s.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/api/product/delete")
async def delete_product(request: Request, _: AdminSession = Depends(check_admin)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    try:
        pid = int(str(form.get("id")))
    except (ValueError, TypeError):
        raise HTTPException(400, "شناسه محصول نامعتبره")
    with Session(engine) as s:
        prod = s.get(Product, pid)
        if prod:
            s.delete(prod)
            s.commit()
    return RedirectResponse("/admin", status_code=303)


# API: sale delete
@app.post("/api/sale/delete")
async def delete_sale(request: Request, _: AdminSession = Depends(check_admin)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    try:
        sid = int(str(form.get("sale_id")))
    except (ValueError, TypeError):
        raise HTTPException(400, "شناسه فروش نامعتبره")
    with Session(engine) as s:
        for v in s.exec(select(SaleValue).where(SaleValue.sale_id == sid)).all():
            s.delete(v)
        sale = s.get(Sale, sid)
        if sale:
            s.delete(sale)
        s.commit()
    return RedirectResponse("/sales", status_code=303)


# API: reports
@app.get("/api/reports/data")
def reports_data(_: AdminSession = Depends(check_admin)):
    with Session(engine) as s:
        total_sales = s.exec(select(func.count(Sale.id))).one()
        product_col = s.exec(select(Column).where(Column.type == "product")).first()
        gender_col = s.exec(select(Column).where(Column.type == "choice")).first()

        products, genders = [], []
        if product_col:
            rows = s.exec(
                select(SaleValue.value, func.count(SaleValue.id))
                .where(SaleValue.column_id == product_col.id)
                .group_by(SaleValue.value)
                .order_by(func.count(SaleValue.id).desc())
                .limit(10)
            ).all()
            products = [{"name": r[0], "count": r[1]} for r in rows]
        if gender_col:
            rows = s.exec(
                select(SaleValue.value, func.count(SaleValue.id))
                .where(SaleValue.column_id == gender_col.id)
                .group_by(SaleValue.value)
            ).all()
            genders = [{"name": r[0], "count": r[1]} for r in rows]

        return {"total_sales": total_sales, "products": products, "genders": genders}


# API: backup
@app.get("/api/backup/download")
def download_backup(_: AdminSession = Depends(check_admin)):
    return FileResponse("database.db", filename="backup.db")


@app.post("/api/backup/restore")
async def restore_backup(request: Request, _: AdminSession = Depends(check_admin)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    file = form["file"]
    if not hasattr(file, "read"):
        raise HTTPException(400, "فایل نامعتبره")
    content = await file.read()  # type: ignore
    if not content:
        raise HTTPException(400, "فایل خالیه")

    import sqlite3
    tmp = "database.restore.tmp"
    with open(tmp, "wb") as f:
        f.write(content)
    try:
        conn = sqlite3.connect(tmp)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        conn.close()
    except sqlite3.DatabaseError:
        import os
        os.remove(tmp)
        raise HTTPException(400, "فایل بک‌آپ معتبر نیست")

    engine.dispose()
    import os
    os.replace(tmp, "database.db")
    engine.dispose()
    SQLModel.metadata.create_all(engine)
    return RedirectResponse("/backup", status_code=303)


# API: CSV export
@app.get("/api/export/csv")
def export_csv(_: AdminSession = Depends(check_admin)):
    with Session(engine) as s:
        columns = _get_columns(s)
        sales = s.exec(select(Sale).order_by(Sale.id)).all()

        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf)
        writer.writerow(["شماره", "تاریخ"] + [c.name for c in columns])
        for sale in sales:
            values = {v.column_id: v.value for v in s.exec(
                select(SaleValue).where(SaleValue.sale_id == sale.id)
            ).all()}
            row = [sale.id, sale.created_at.strftime("%Y-%m-%d %H:%M")]
            for c in columns:
                row.append(values.get(c.id, ""))
            writer.writerow(row)

        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="sales.csv"'},
        )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
