"""Seniority (hire date) proposal/approval flow tests (TDD).

Run: uv run --with httpx python tests/test_seniority_flow.py

Behavior under test:
- employees propose a hire date from their profile; it does NOT count as
  seniority until an admin approves it
- admin approve -> hire_date set (counts for favorite-desk contests)
- admin reject -> proposal cleared
- admin direct edit of hire_date clears any pending proposal
- old DBs get the new column via init_db migration
"""
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_db_file = tempfile.mktemp(suffix=".db")
os.environ["HOTDESK_DB"] = _db_file
os.environ.pop("SMTP_HOST", None)

# Pre-create the OLD production schema (no password columns, no purpose, no
# hire_date_proposed) so the whole suite runs against a migrated DB.
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
from app.dates import upcoming_weekdays  # noqa: E402
from app.assignment import is_day_locked  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Booking, Desk, Employee, MagicLink  # noqa: E402

RESULTS = []


def report(name, ok, detail):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def unlocked_days():
    days = [d for d in upcoming_weekdays() if not is_day_locked(d)]
    if not days:
        raise RuntimeError("no unlocked day in window")
    return days


def get_token(email):
    with Session(engine) as s:
        return s.exec(select(MagicLink).where(MagicLink.email == email)).first().token


def login_employee(client, email, name="Emp", surname="X", team_id="1", favorite_desk_id="", hire_date=""):
    client.cookies.clear()
    r = client.post("/login", data={"email": email})
    assert r.status_code == 200, r.text
    token = get_token(email)
    r = client.get(f"/auth/verify?token={token}")
    assert r.status_code in (302, 303, 307), r.status_code
    r = client.post("/set-password", data={"token": token, "new_password": "test-Pass1!", "confirm_password": "test-Pass1!"})
    assert r.status_code == 303, r.text
    r = client.post("/profile", data={
        "name": name, "surname": surname, "team_id": team_id,
        "favorite_desk_id": favorite_desk_id, "hire_date": hire_date,
    })
    assert r.status_code in (302, 303, 307), r.text


def admin_login(client):
    r = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
    assert r.status_code == 303, r.status_code


def emp(email):
    with Session(engine) as s:
        e = s.exec(select(Employee).where(Employee.email == email)).first()
        s.refresh(e)
        return (e.id, e.hire_date, e.hire_date_proposed)


def p01_holder(day):
    with Session(engine) as s:
        p01 = s.exec(select(Desk).where(Desk.code == "P01")).first().id
        b = s.exec(select(Booking).where(Booking.day == day, Booking.desk_id == p01)).first()
        if not b:
            return None
        e = s.get(Employee, b.employee_id)
        return e.email


def book(client, day, mode="presencial"):
    r = client.post("/calendar/book", data={"day": day.isoformat(), "mode": mode})
    assert r.status_code == 200, r.text


def test_proposal_does_not_count_until_approved():
    day = unlocked_days()[0]
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        admin_login(client)
        with Session(engine) as s:
            p01 = s.exec(select(Desk).where(Desk.code == "P01")).first().id
        # B declares first, A second; A has a PENDING 2000 hire date proposal
        login_employee(client, "senb@example.com", name="B", favorite_desk_id=str(p01))
        book(client, day)
        login_employee(client, "sena@example.com", name="A", favorite_desk_id=str(p01), hire_date="2000-01-01")
        book(client, day)
        holder = p01_holder(day)
        report("propuesta pendiente no cuenta como seniority", holder == "senb@example.com",
               f"titular de P01: {holder} (esperado senb: B declaro primero y la propuesta de A no debe contar)")


def test_admin_approve_makes_it_count():
    day = unlocked_days()[1]
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        admin_login(client)
        with Session(engine) as s:
            p01 = s.exec(select(Desk).where(Desk.code == "P01")).first().id
        login_employee(client, "apb@example.com", name="B", favorite_desk_id=str(p01))
        book(client, day)
        login_employee(client, "apa@example.com", name="A", favorite_desk_id=str(p01), hire_date="2000-01-01")
        book(client, day)
        aid, _, pending = emp("apa@example.com")
        assert pending == date(2000, 1, 1), f"pendiente no guardada: {pending}"
        admin_login(client)  # login_employee limpio la cookie de admin
        r = client.post(f"/admin/employees/{aid}/hire_date_proposed", data={"action": "approve"})
        assert r.status_code in (302, 303), r.text
        _, approved, pending_after = emp("apa@example.com")
        # re-resolve: a third employee toggles
        login_employee(client, "apc@example.com", name="C")
        book(client, day, "teletrabajo")
        book(client, day)
        holder = p01_holder(day)
        report("approve -> cuenta como seniority",
               approved == date(2000, 1, 1) and pending_after is None and holder == "apa@example.com",
               f"hire_date={approved}, pendiente={pending_after}, titular P01={holder} (esperado apa)")


def test_admin_reject_clears_proposal():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        admin_login(client)
        with Session(engine) as s:
            p01 = s.exec(select(Desk).where(Desk.code == "P01")).first().id
        login_employee(client, "rja@example.com", name="A", favorite_desk_id=str(p01), hire_date="1999-05-05")
        aid, approved_before, pending = emp("rja@example.com")
        assert pending == date(1999, 5, 5)
        admin_login(client)
        r = client.post(f"/admin/employees/{aid}/hire_date_proposed", data={"action": "reject"})
        assert r.status_code in (302, 303), r.text
        _, approved_after, pending_after = emp("rja@example.com")
        report("reject -> limpia propuesta",
               pending_after is None and approved_after == approved_before,
               f"pendiente={pending_after}, hire_date={approved_after} (esperado None / sin cambios)")


def test_admin_direct_edit_clears_pending():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        admin_login(client)
        with Session(engine) as s:
            p01 = s.exec(select(Desk).where(Desk.code == "P01")).first().id
        login_employee(client, "dta@example.com", name="A", favorite_desk_id=str(p01), hire_date="1995-01-01")
        aid, _, pending = emp("dta@example.com")
        assert pending == date(1995, 1, 1)
        admin_login(client)
        r = client.post(f"/admin/employees/{aid}/edit", data={
            "name": "A", "surname": "X", "team_id": "1", "hire_date": "2010-06-15",
        })
        assert r.status_code in (302, 303), r.text
        _, approved, pending_after = emp("dta@example.com")
        report("edicion directa del admin limpia pendiente",
               approved == date(2010, 6, 15) and pending_after is None,
               f"hire_date={approved}, pendiente={pending_after} (esperado 2010-06-15 / None)")


def test_admin_list_shows_pending_proposal():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        admin_login(client)
        with Session(engine) as s:
            p01 = s.exec(select(Desk).where(Desk.code == "P01")).first().id
        login_employee(client, "lst@example.com", name="L", favorite_desk_id=str(p01), hire_date="2001-02-03")
        admin_login(client)
        r = client.get("/admin/employees")
        assert r.status_code == 200
        ok = "2001-02-03" in r.text and "hire_date_proposed" in r.text
        report("admin ve la propuesta pendiente con acciones", ok,
               f"lista de empleados muestra 2001-02-03 y acciones: {ok}")


def test_old_schema_migration():
    con = sqlite3.connect(_db_file)
    emp_cols = {r[1] for r in con.execute("PRAGMA table_info(employee)")}
    con.close()
    ok = "hire_date_proposed" in emp_cols
    report("migracion esquema antiguo (hire_date_proposed)", ok, f"columna presente: {ok}")


if __name__ == "__main__":
    import traceback

    for test in (
        test_proposal_does_not_count_until_approved,
        test_admin_approve_makes_it_count,
        test_admin_reject_clears_proposal,
        test_admin_direct_edit_clears_pending,
        test_admin_list_shows_pending_proposal,
        test_old_schema_migration,
    ):
        try:
            test()
        except Exception:
            report(test.__name__, False, traceback.format_exc().strip().splitlines()[-1])
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n=== RESUMEN: {len(RESULTS) - len(failed)}/{len(RESULTS)} pasando; fallando: {failed or 'ninguno'} ===")
    sys.exit(1 if failed else 0)
