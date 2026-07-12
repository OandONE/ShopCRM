from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Session, create_engine, select
from models import Admin, Column, Product, SaleValue
from contextlib import asynccontextmanager
import uvicorn
import hashlib
import secrets
import os

# Setup
CSRF_SECRET = secrets.token_hex(32)

@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        if not session.exec(select(Admin)).first():
            session.add(Admin(username="admin", password_hash=hashlib.sha256("admin".encode()).hexdigest()))
        if not session.exec(select(Column)).first():
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
        if not session.exec(select(Product)).first():
            for p in ["پنیر", "شیر", "ماست", "دوغ", "کره", "خامه", "عسل", "نان", "برنج", "روغن"]:
                session.add(Product(name=p))
            session.commit()
    yield

app = FastAPI(lifespan=lifespan)
engine = create_engine("sqlite:///database.db")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Auth
ADMIN_COOKIE = "admin_session"

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def check_admin(request: Request):
    token = request.cookies.get(ADMIN_COOKIE)
    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    with Session(engine) as session:
        admin = session.exec(select(Admin).where(Admin.password_hash == token)).first()
        if not admin:
            raise HTTPException(status_code=302, headers={"Location": "/login"})
    return True

def generate_csrf() -> str:
    return secrets.token_hex(16)

# Pages
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error, "csrf": generate_csrf()})

@app.post("/login")
async def login(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    with Session(engine) as session:
        admin = session.exec(select(Admin).where(Admin.username == username)).first()
        if admin and admin.password_hash == hash_password(password):
            resp = RedirectResponse("/admin", status_code=303)
            resp.set_cookie(ADMIN_COOKIE, admin.password_hash, httponly=True, samesite="lax")
            return resp
    return templates.TemplateResponse("login.html", {"request": request, "error": "نام کاربری یا رمز اشتباه", "csrf": generate_csrf()})

@app.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp

@app.get("/quick", response_class=HTMLResponse)
def quick_form(request: Request):
    with Session(engine) as session:
        columns = session.exec(select(Column).where(Column.is_default == True).order_by(Column.order.asc())).all()  # type: ignore
        products = session.exec(select(Product).order_by(Product.name)).all()
    return templates.TemplateResponse("quick.html", {"request": request, "columns": columns, "products": products})

@app.get("/full", response_class=HTMLResponse)
def full_form(request: Request):
    with Session(engine) as session:
        columns = session.exec(select(Column).order_by(Column.order.asc())).all()  # type: ignore
        products = session.exec(select(Product).order_by(Product.name)).all()
    return templates.TemplateResponse("full.html", {"request": request, "columns": columns, "products": products})

@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request):
    return templates.TemplateResponse("search.html", {"request": request})

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, _=Depends(check_admin)):
    with Session(engine) as session:
        columns = session.exec(select(Column).order_by(Column.order.asc())).all()  # type: ignore
        products = session.exec(select(Product).order_by(Product.name)).all()
    return templates.TemplateResponse("admin.html", {"request": request, "columns": columns, "products": products, "csrf": generate_csrf()})

@app.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request, _=Depends(check_admin)):
    return templates.TemplateResponse("backup.html", {"request": request})

# API

# Number to Persian Words
PERSIAN_NUMBERS = ["صفر","یک","دو","سه","چهار","پنج","شش","هفت","هشت","نه"]
PERSIAN_TENS = ["","ده","بیست","سی","چهل","پنجاه","شصت","هفتاد","هشتاد","نود"]
PERSIAN_HUNDREDS = ["","صد","دویست","سیصد","چهارصد","پانصد","ششصد","هفتصد","هشتصد","نهصد"]
PERSIAN_UNITS = ["","هزار","میلیون","میلیارد"]

def number_to_persian(n: int) -> str:
    if n == 0:
        return PERSIAN_NUMBERS[0]
    
    def convert_below_1000(num):
        if num == 0:
            return ""
        result = ""
        if num >= 100:
            result += PERSIAN_HUNDREDS[num // 100] + " "
            num %= 100
        if num >= 10:
            result += PERSIAN_TENS[num // 10] + " "
            num %= 10
        if num > 0:
            result += PERSIAN_NUMBERS[num] + " "
        return result.strip()
    
    parts = []
    unit_idx = 0
    while n > 0:
        part = n % 1000
        if part > 0:
            part_str = convert_below_1000(part)
            if unit_idx > 0:
                part_str += " " + PERSIAN_UNITS[unit_idx]
            parts.append(part_str)
        n //= 1000
        unit_idx += 1
    
    parts.reverse()
    return " و ".join(parts)


@app.get("/api/price-to-words")
def price_to_words(price: str = ""):
    try:
        p = int(price)
        return {"words": number_to_persian(p) + " تومان"}
    except:
        return {"words": ""}

@app.post("/api/sale")
async def save_sale(request: Request):
    form = await request.form()
    with Session(engine) as session:
        all_sales = session.exec(select(SaleValue.sale_id)).all()
        sale_id = max(all_sales) + 1 if all_sales else 1
        for key, value in form.items():
            if key.startswith("col_") and key != "col_datetime":
                column_id = int(key.replace("col_", ""))
                sv = SaleValue(sale_id=sale_id, column_id=column_id, value=str(value))
                session.add(sv)
                session.commit()
    return RedirectResponse("/quick", status_code=303)

@app.get("/api/search-products")
def search_products(q: str = ""):
    with Session(engine) as session:
        if q:
            products = session.exec(select(Product).where(Product.name.like(f"%{q}%"))).all()  # type: ignore
        else:
            products = session.exec(select(Product).order_by(Product.name)).all()
        return [{"id": p.id, "name": p.name} for p in products]

@app.get("/api/search")
def search_api(q: str = ""):
    with Session(engine) as session:
        if q:
            values = session.exec(select(SaleValue).where(SaleValue.value.like(f"%{q}%"))).all()  # type: ignore
        else:
            values = session.exec(select(SaleValue)).all()
        columns = {c.id: c.name for c in session.exec(select(Column)).all()}
        sales = {}
        for v in values:
            if v.sale_id not in sales:
                sales[v.sale_id] = {"sale_id": v.sale_id, "data": {}}
            col_name = columns.get(v.column_id, f"ستون {v.column_id}")
            sales[v.sale_id]["data"][col_name] = v.value
        return list(sales.values())

@app.post("/api/column/add")
async def add_column(request: Request):
    check_admin(request)
    form = await request.form()
    with Session(engine) as session:
        col = Column(
            name=str(form.get("name", "ستون جدید")),
            type=str(form.get("type", "text")),
            options=str(form.get("options")) if form.get("options") else None,
            required=form.get("required") == "on",
            is_default=form.get("is_default") == "on",
            order=len(session.exec(select(Column)).all())
        )
        session.add(col)
        session.commit()
    return RedirectResponse("/admin", status_code=303)

@app.post("/api/column/delete")
async def delete_column(request: Request):
    check_admin(request)
    form = await request.form()
    col_id = int(str(form.get("id")))
    with Session(engine) as session:
        col = session.get(Column, col_id)
        if col:
            session.delete(col)
            session.commit()
    return RedirectResponse("/admin", status_code=303)

@app.post("/api/product/add")
async def add_product(request: Request):
    check_admin(request)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        with Session(engine) as session:
            if not session.exec(select(Product).where(Product.name == name)).first():
                session.add(Product(name=name))
                session.commit()
    return RedirectResponse("/admin", status_code=303)

@app.post("/api/product/delete")
async def delete_product(request: Request):
    check_admin(request)
    form = await request.form()
    prod_id = int(str(form.get("id")))
    with Session(engine) as session:
        prod = session.get(Product, prod_id)
        if prod:
            session.delete(prod)
            session.commit()
    return RedirectResponse("/admin", status_code=303)

@app.get("/api/backup/download")
def download_backup(_=Depends(check_admin)):
    return FileResponse("database.db", filename="backup.db")

@app.post("/api/backup/restore")
async def restore_backup(request: Request):
    check_admin(request)
    form = await request.form()
    file = form["file"]
    if hasattr(file, "read"):  # type: ignore
        content = await file.read()
        with open("database.db", "wb") as f:
            f.write(content)
    return RedirectResponse("/backup", status_code=303)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)

