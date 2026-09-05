"""Password login tests (TDD). Run: uv run --with httpx python tests/test_password_login.py

Behavior under test:
- magic link is only for first registration / password reset
- first login forces choosing a password (/set-password)
- afterwards, login is email+password
- "forgot password" sends a reset magic link that sets a new password
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_db_file = tempfile.mktemp(suffix=".db")
os.environ["HOTDESK_DB"] = _db_file
os.environ.pop("SMTP_HOST", None)

# Pre-create the OLD production schema (no password columns) so the whole
# suite runs against a migrated DB — init_db must add the new columns.
sqlite3.connect(_db_file).executescript(
    """
    CREATE TABLE team (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, is_helpdesk BOOLEAN NOT NULL);
    CREATE TABLE zone (id INTEGER PRIMARY KEY, code VARCHAR NOT NULL, name VARCHAR NOT NULL);
    CREATE TABLE desk (id INTEGER PRIMARY KEY, code VARCHAR NOT NULL, zone_id INTEGER NOT NULL REFERENCES zone(id), reserved VARCHAR NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL);
    CREATE TABLE employee (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL, name VARCHAR, surname VARCHAR, team_id INTEGER REFERENCES team(id), favorite_desk_id INTEGER REFERENCES desk(id), is_boss BOOLEAN NOT NULL, hire_date DATE, created_at DATETIME NOT NULL);
    CREATE TABLE booking (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employee(id), day DATE NOT NULL, mode VARCHAR NOT NULL, desk_id INTEGER REFERENCES desk(id), status VARCHAR, created_at DATETIME NOT NULL);
    CREATE TABLE magiclink (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL, token VARCHAR NOT NULL, expires_at DATETIME NOT NULL, used_at DATETIME);
    CREATE TABLE adminuser (id INTEGER PRIMARY KEY, username VARCHAR NOT NULL, password_hash VARCHAR NOT NULL, password_salt VARCHAR NOT NULL, created_at DATETIME NOT NULL);
    """
).close()

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Employee, MagicLink  # noqa: E402

RESULTS = []


def report(name, ok, detail):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def get_token(email, purpose=None):
    with Session(engine) as s:
        q = select(MagicLink).where(MagicLink.email == email)
        if purpose:
            q = q.where(MagicLink.purpose == purpose)
        return s.exec(q).first().token


def request_link(client, email, purpose="login"):
    """POST /login (or /forgot for reset) and return the issued token."""
    if purpose == "login":
        r = client.post("/login", data={"email": email})
    else:
        r = client.post("/forgot", data={"email": email})
    assert r.status_code == 200, f"{purpose} link request -> {r.status_code}: {r.text[:200]}"
    return get_token(email, purpose)


def verify(client, token):
    r = client.get(f"/auth/verify?token={token}")
    assert r.status_code in (302, 303, 307), f"verify -> {r.status_code}: {r.text[:200]}"
    return r.headers.get("location", "")


def set_password(client, token, new="sup3r-Secret!", confirm=None):
    return client.post(
        "/set-password",
        data={"token": token, "new_password": new, "confirm_password": confirm if confirm is not None else new},
    )


def admin_login(client):
    r = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
    assert r.status_code == 303, r.status_code


def test_registration_forces_set_password():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        token = request_link(client, "reg@example.com")
        location = verify(client, token)
        report("registro redirige a /set-password", location.startswith("/set-password"),
               f"verify -> {location!r} (esperado /set-password?token=...)")


def test_set_password_creates_credentials_and_session():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        token = request_link(client, "setpw@example.com")
        verify(client, token)
        r = set_password(client, token)
        with Session(engine) as s:
            emp = s.exec(select(Employee).where(Employee.email == "setpw@example.com")).first()
            has_hash = bool(emp.password_hash and emp.password_salt)
        cookie = "session" in r.cookies or any("session=" in c for c in r.headers.get_list("set-cookie"))
        report("set-password crea credenciales + sesion", r.status_code == 303 and has_hash and cookie,
               f"POST /set-password -> {r.status_code}, hash en DB: {has_hash}, cookie: {cookie}")


def test_login_with_password():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        token = request_link(client, "pwlogin@example.com")
        verify(client, token)
        assert set_password(client, token).status_code == 303
        client.cookies.clear()
        r = client.post("/login", data={"email": "pwlogin@example.com", "password": "sup3r-Secret!"})
        cookie = any("session=" in c for c in r.headers.get_list("set-cookie"))
        report("login con password correcto", r.status_code == 303 and cookie,
               f"POST /login -> {r.status_code}, cookie: {cookie}")


def test_login_wrong_password():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        token = request_link(client, "wrongpw@example.com")
        verify(client, token)
        assert set_password(client, token).status_code == 303
        client.cookies.clear()
        r = client.post("/login", data={"email": "wrongpw@example.com", "password": "nope-nope"})
        cookie = any("session=" in c for c in r.headers.get_list("set-cookie"))
        report("login con password incorrecto", r.status_code == 200 and not cookie and "Invalid email or password" in r.text,
               f"POST /login -> {r.status_code} (esperado 200 anti-enumeración), cookie: {cookie}, error visible: {'Invalid email or password' in r.text}")


def test_forgot_password_resets():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        token = request_link(client, "forgot@example.com")
        verify(client, token)
        assert set_password(client, token, new="primer-Pass1!").status_code == 303
        client.cookies.clear()
        r = client.post("/forgot", data={"email": "forgot@example.com"})
        assert r.status_code == 200
        reset_token = get_token("forgot@example.com", "reset")
        location = verify(client, reset_token)
        assert location.startswith("/set-password"), location
        r = set_password(client, reset_token, new="segund-Pass2!")
        assert r.status_code == 303, r.text[:200]
        client.cookies.clear()
        r_old = client.post("/login", data={"email": "forgot@example.com", "password": "primer-Pass1!"})
        r_new = client.post("/login", data={"email": "forgot@example.com", "password": "segund-Pass2!"})
        report("forgot password resetea", r_old.status_code == 200 and r_new.status_code == 303,
               f"password vieja -> {r_old.status_code} (esperado 200 sin sesion), nueva -> {r_new.status_code} (esperado 303)")


def test_legacy_no_password_gets_login_link():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        admin_login(client)
        r = client.post("/admin/employees", data={"email": "legacy@example.com", "name": "L", "surname": "E", "team_id": "1"})
        assert r.status_code == 303, r.status_code
        client.cookies.clear()
        r = client.post("/login", data={"email": "legacy@example.com", "password": "lo-que-sea"})
        assert r.status_code == 200, r.text[:200]
        location = verify(client, get_token("legacy@example.com"))
        report("cuenta legacy sin password -> link -> /set-password", location.startswith("/set-password"),
               f"verify -> {location!r}")


def test_set_password_validation():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        token = request_link(client, "valid@example.com")
        verify(client, token)
        r_short = set_password(client, token, new="corta")
        r_mismatch = set_password(client, token, new="buena-Pass1!", confirm="otra-cosa")
        with Session(engine) as s:
            emp = s.exec(select(Employee).where(Employee.email == "valid@example.com")).first()
            has_hash = bool(emp.password_hash)
        report("set-password valida longitud y confirmacion",
               r_short.status_code == 400 and r_mismatch.status_code == 400 and not has_hash,
               f"corta -> {r_short.status_code}, mismatch -> {r_mismatch.status_code}, hash en DB: {has_hash}")


def test_set_password_token_single_use():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        token = request_link(client, "once@example.com")
        verify(client, token)
        r1 = set_password(client, token)
        r2 = set_password(client, token)
        report("token de set-password single-use", r1.status_code == 303 and r2.status_code == 400,
               f"1er uso -> {r1.status_code}, 2o uso -> {r2.status_code} (esperado 303, 400)")


def test_login_purpose_link_with_password_enters_app():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        from datetime import datetime, timedelta
        token = request_link(client, "haspw@example.com")
        verify(client, token)
        assert set_password(client, token).status_code == 303
        # link purpose=login para un usuario que YA tiene password (defensivo)
        with Session(engine) as s:
            s.add(MagicLink(email="haspw@example.com", token="forced-login-link",
                            expires_at=datetime.utcnow() + timedelta(minutes=10), purpose="login"))
            s.commit()
        location = verify(client, "forced-login-link")
        report("link login con password existente -> app", location in ("/map", "/settings"),
               f"verify -> {location!r}")


def count_links(email):
    with Session(engine) as s:
        return len(s.exec(select(MagicLink).where(MagicLink.email == email)).all())


def test_empty_password_not_a_bypass():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        token = request_link(client, "bypass@example.com")
        verify(client, token)
        assert set_password(client, token).status_code == 303
        client.cookies.clear()
        before = count_links("bypass@example.com")
        r = client.post("/login", data={"email": "bypass@example.com", "password": ""})
        after = count_links("bypass@example.com")
        cookie = any("session=" in c for c in r.headers.get_list("set-cookie"))
        report("password vacio no es bypass ni envia link",
               r.status_code == 200 and not cookie and after == before,
               f"-> {r.status_code}, cookie: {cookie}, links antes/después: {before}/{after}")


def test_failed_validation_keeps_token():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        token = request_link(client, "retry@example.com")
        verify(client, token)
        r1 = set_password(client, token, new="corta")
        r2 = set_password(client, token, new="buena-Pass1!")
        report("validacion fallida no consume el token", r1.status_code == 400 and r2.status_code == 303,
               f"1er intento (corta) -> {r1.status_code}, reintento -> {r2.status_code}")


def test_expired_token_rejected():
    from datetime import datetime, timedelta
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        token = request_link(client, "expired@example.com")
        with Session(engine) as s:
            link = s.exec(select(MagicLink).where(MagicLink.token == token)).first()
            link.expires_at = datetime.utcnow() - timedelta(minutes=1)
            s.add(link)
            s.commit()
        r = client.get(f"/auth/verify?token={token}")
        report("token expirado rechazado en verify", r.status_code == 400,
               f"GET /auth/verify -> {r.status_code} (esperado 400)")


def test_old_schema_migration():
    # The DB was pre-created with the old schema at module import; init_db
    # (run on first TestClient startup) must have added the new columns.
    con = sqlite3.connect(_db_file)
    emp_cols = {r[1] for r in con.execute("PRAGMA table_info(employee)")}
    ml_cols = {r[1] for r in con.execute("PRAGMA table_info(magiclink)")}
    con.close()
    ok = {"password_hash", "password_salt"} <= emp_cols and "purpose" in ml_cols
    report("migracion esquema antiguo", ok,
           f"employee cols nuevas: {emp_cols & {'password_hash', 'password_salt'}}, magiclink: {'purpose' in ml_cols}")


if __name__ == "__main__":
    import traceback

    for test in (
        test_registration_forces_set_password,
        test_set_password_creates_credentials_and_session,
        test_login_with_password,
        test_login_wrong_password,
        test_forgot_password_resets,
        test_legacy_no_password_gets_login_link,
        test_set_password_validation,
        test_set_password_token_single_use,
        test_login_purpose_link_with_password_enters_app,
        test_empty_password_not_a_bypass,
        test_failed_validation_keeps_token,
        test_expired_token_rejected,
        test_old_schema_migration,
    ):
        try:
            test()
        except Exception:
            report(test.__name__, False, traceback.format_exc().strip().splitlines()[-1])
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n=== RESUMEN: {len(RESULTS) - len(failed)}/{len(RESULTS)} pasando; fallando: {failed or 'ninguno'} ===")
    sys.exit(1 if failed else 0)
