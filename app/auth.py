import secrets
from datetime import datetime, timedelta

from fastapi import Cookie, Depends, HTTPException
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlmodel import Session, select

from app.db import get_session
from app.email import send_email
from app.models import Employee, MagicLink
from app.security import SECRET_KEY, hash_password

serializer = URLSafeTimedSerializer(SECRET_KEY)

MAGIC_LINK_TTL = timedelta(minutes=15)
SESSION_COOKIE = "session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def create_magic_link(session: Session, email: str, purpose: str = "login") -> str:
    token = secrets.token_urlsafe(32)
    link = MagicLink(email=email, token=token, expires_at=datetime.utcnow() + MAGIC_LINK_TTL, purpose=purpose)
    session.add(link)
    session.commit()
    return token


def request_login(session: Session, email: str, base_url: str) -> None:
    token = create_magic_link(session, email)
    url = f"{base_url.rstrip('/')}/auth/verify?token={token}"
    send_email(email, "Your Hotdesk login link", f"Sign in here (valid for 15 minutes): {url}")


def request_reset(session: Session, email: str, base_url: str) -> None:
    token = create_magic_link(session, email, purpose="reset")
    url = f"{base_url.rstrip('/')}/auth/verify?token={token}"
    send_email(email, "Reset your Hotdesk password", f"Set a new password here (valid for 15 minutes): {url}")


def verify_token(session: Session, token: str, consume: bool = True) -> tuple[MagicLink, Employee]:
    link = session.exec(select(MagicLink).where(MagicLink.token == token)).first()
    if not link or link.used_at or link.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired link")

    employee = session.exec(select(Employee).where(Employee.email == link.email)).first()
    if not employee:
        employee = Employee(email=link.email)
        session.add(employee)
    if consume:
        link.used_at = datetime.utcnow()
        session.add(link)
    session.commit()
    session.refresh(employee)
    return link, employee


def set_password(session: Session, token: str, new_password: str) -> Employee:
    """Consume a (login or reset) magic link and store the new password."""
    _, employee = verify_token(session, token, consume=True)
    employee.password_hash, employee.password_salt = hash_password(new_password)
    session.add(employee)
    session.commit()
    return employee


def make_session_cookie(employee_id: int) -> str:
    return serializer.dumps({"employee_id": employee_id})


def _employee_from_cookie(session: Session, session_cookie: str | None) -> Employee | None:
    if not session_cookie:
        return None
    try:
        data = serializer.loads(session_cookie, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return session.get(Employee, data["employee_id"])


def current_employee(
    session: Session = Depends(get_session),
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> Employee:
    employee = _employee_from_cookie(session, session_cookie)
    if not employee:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return employee


def optional_employee(
    session: Session = Depends(get_session),
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> Employee | None:
    return _employee_from_cookie(session, session_cookie)
