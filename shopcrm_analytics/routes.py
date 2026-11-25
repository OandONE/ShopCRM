"""FastAPI routes for the ShopCRM advanced analytics subsystem"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlmodel import Session, select

from models import SavedAnalysis, User
from .engine import (
    AnalyticsValidationError,
    get_field_catalog,
    normalize_config,
    run_analysis,
)


def _json_response(payload: dict, *, download: bool = False) -> Response:
    content = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="shopcrm-analytics.json"'
    return Response(content=content, media_type="application/json; charset=utf-8", headers=headers)


def build_analytics_router(*, engine, render, require_permission, verify_csrf_header):
    router = APIRouter()
    reports_permission = require_permission("view_reports")

    @router.get("/analytics", response_class=HTMLResponse)
    def analytics_page(request: Request, user: User = Depends(reports_permission)):
        return render(request, "analytics.html")

    @router.get("/api/analytics/fields")
    def analytics_fields(user: User = Depends(reports_permission)):
        with Session(engine) as session:
            return get_field_catalog(
                session,
                can_view_purchase_cost=user.has_permission("view_purchase_cost"),
            )

    def _run(body: dict, user: User) -> dict:
        config = body.get("config", body) if isinstance(body, dict) else body
        try:
            with Session(engine) as session:
                return run_analysis(
                    session,
                    config,
                    can_view_purchase_cost=user.has_permission("view_purchase_cost"),
                )
        except AnalyticsValidationError as exc:
            raise HTTPException(400, str(exc))

    @router.post("/api/analytics/run")
    async def analytics_run(request: Request, user: User = Depends(reports_permission)):
        verify_csrf_header(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "بدنهٔ درخواست JSON معتبر نیست")
        result = _run(body, user)
        return _json_response(result)

    @router.post("/api/analytics/export.json")
    async def analytics_export_json(request: Request, user: User = Depends(reports_permission)):
        verify_csrf_header(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "بدنهٔ درخواست JSON معتبر نیست")
        return _json_response(_run(body, user), download=True)

    @router.get("/api/analytics/saved")
    def analytics_saved_list(user: User = Depends(reports_permission)):
        with Session(engine) as session:
            rows = session.exec(
                select(SavedAnalysis)
                .where(SavedAnalysis.user_id == user.id)
                .order_by(SavedAnalysis.updated_at.desc(), SavedAnalysis.id.desc())
            ).all()
        return [{
            "id": row.id,
            "name": row.name,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        } for row in rows]

    @router.post("/api/analytics/saved")
    async def analytics_saved_create(request: Request, user: User = Depends(reports_permission)):
        verify_csrf_header(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "بدنهٔ درخواست JSON معتبر نیست")
        if not isinstance(body, dict):
            raise HTTPException(400, "اطلاعات تحلیل ذخیره‌شده معتبر نیست")
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 80:
            raise HTTPException(400, "نام تحلیل باید بین ۱ تا ۸۰ نویسه باشد")
        try:
            config = normalize_config(body.get("config"))
            if config["metric"] in {"purchase_cost", "estimated_profit"} and not user.has_permission("view_purchase_cost"):
                raise AnalyticsValidationError("به معیار انتخاب‌شده دسترسی ندارید")
        except AnalyticsValidationError as exc:
            raise HTTPException(400, str(exc))

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with Session(engine) as session:
            saved_id = body.get("id")
            saved = None
            if saved_id is not None:
                try:
                    saved_id = int(saved_id)
                except (TypeError, ValueError):
                    raise HTTPException(400, "شناسهٔ تحلیل معتبر نیست")
                saved = session.exec(select(SavedAnalysis).where(
                    SavedAnalysis.id == saved_id,
                    SavedAnalysis.user_id == user.id,
                )).first()
                if not saved:
                    raise HTTPException(404, "تحلیل ذخیره‌شده پیدا نشد")
                saved.name = name
                saved.config_json = json.dumps(config, ensure_ascii=False)
                saved.updated_at = now
            else:
                saved = SavedAnalysis(
                    user_id=user.id,
                    name=name,
                    config_json=json.dumps(config, ensure_ascii=False),
                    created_at=now,
                    updated_at=now,
                )
                session.add(saved)
            session.commit()
            session.refresh(saved)
            return {"id": saved.id, "name": saved.name}

    @router.get("/api/analytics/saved/{analysis_id}")
    def analytics_saved_get(analysis_id: int, user: User = Depends(reports_permission)):
        with Session(engine) as session:
            saved = session.exec(
                select(SavedAnalysis).where(
                    SavedAnalysis.id == analysis_id,
                    SavedAnalysis.user_id == user.id,
                )
            ).first()
        if not saved:
            raise HTTPException(404, "تحلیل ذخیره‌شده پیدا نشد")
        try:
            config = json.loads(saved.config_json)
        except (TypeError, ValueError):
            raise HTTPException(500, "تنظیمات تحلیل ذخیره‌شده قابل خواندن نیست")
        return {"id": saved.id, "name": saved.name, "config": config}

    @router.delete("/api/analytics/saved/{analysis_id}")
    async def analytics_saved_delete(analysis_id: int, request: Request, user: User = Depends(reports_permission)):
        verify_csrf_header(request)
        with Session(engine) as session:
            saved = session.exec(
                select(SavedAnalysis).where(
                    SavedAnalysis.id == analysis_id,
                    SavedAnalysis.user_id == user.id,
                )
            ).first()
            if not saved:
                raise HTTPException(404, "تحلیل ذخیره‌شده پیدا نشد")
            session.delete(saved)
            session.commit()
        return {"ok": True}

    return router
