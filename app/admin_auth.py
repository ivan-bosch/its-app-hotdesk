"""Admin authentication: username/password login, separate from the employee
magic-link system in auth.py. Uses its own cookie (`admin_session`) so an
employee session and an admin session can coexist in the same browser."""

from fastapi import Cookie, Depends, HTTPException
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlmodel import Session, select

from app.db import get_session
from app.models import AdminUser
from app.security import SECRET_KEY, verify_password

serializer = URLSafeTimedSerializer(SECRET_KEY, salt="admin-session")

ADMIN_SESSION_COOKIE = "admin_session"
ADMIN_SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours


def make_admin_session_cookie(admin_id: int) -> str:
    return serializer.dumps({"admin_id": admin_id})


def _admin_from_cookie(session: Session, cookie: str | None) -> AdminUser | None:
    if not cookie:
        return None
    try:
        data = serializer.loads(cookie, max_age=ADMIN_SESSION_MAX_AGE)
    except BadSignature:
        return None
    return session.get(AdminUser, data["admin_id"])


def current_admin(
    session: Session = Depends(get_session),
    admin_session: str | None = Cookie(default=None, alias=ADMIN_SESSION_COOKIE),
) -> AdminUser:
    admin = _admin_from_cookie(session, admin_session)
    if not admin:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return admin


def optional_admin(
    session: Session = Depends(get_session),
    admin_session: str | None = Cookie(default=None, alias=ADMIN_SESSION_COOKIE),
) -> AdminUser | None:
    return _admin_from_cookie(session, admin_session)


def authenticate_admin(session: Session, username: str, password: str) -> AdminUser | None:
    admin = session.exec(select(AdminUser).where(AdminUser.username == username)).first()
    if not admin or not verify_password(password, admin.password_hash, admin.password_salt):
        return None
    return admin
