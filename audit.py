"""ثبت لاگ عملیات در جدول AuditLog"""

from typing import Optional
from fastapi import Request
from sqlmodel import Session
from models import AuditLog, User
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _client_ip(request: Optional[Request]) -> Optional[str]:
    if not request:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def write_audit(
    engine,
    request: Optional[Request],
    action: str,
    user: Optional[User] = None,
    username: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    metadata_json: Optional[str] = None,
    success: bool = True,
    error_message: Optional[str] = None,
) -> None:
    """
    یه ردیف AuditLog می‌نویسه و بلافاصله commit می‌کنه.
    هرگز raise نمی‌کنه — اگه log شکست بخوره، جریان اصلی ادامه پیدا می‌کنه.
    """
    try:
        if user is not None:
            user_id = user.id
            username_snapshot = user.username
        else:
            user_id = None
            username_snapshot = username or "(ناشناس)"

        ip = _client_ip(request)
        ua = request.headers.get("user-agent") if request else None
        lang = request.headers.get("accept-language") if request else None
        referer = request.headers.get("referer") if request else None
        tz = request.cookies.get("tz") if request else None

        entry = AuditLog(
            user_id=user_id,
            username_snapshot=username_snapshot,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata_json=metadata_json,
            success=success,
            error_message=error_message,
            ip_address=ip,
            user_agent=ua,
            accept_language=lang,
            referer=referer,
            timezone=tz,
            created_at=_utcnow(),
        )
        with Session(engine) as s:
            s.add(entry)
            s.commit()
    except Exception:
        
        import traceback
        traceback.print_exc()
