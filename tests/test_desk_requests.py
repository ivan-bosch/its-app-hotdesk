"""Desk request (seat cede) flow tests (TDD).

Run: uv run --with httpx python tests/test_desk_requests.py

Behavior under test:
- an employee can request an occupied desk from the map (for the shown day);
  the occupant gets an email with a link to accept/decline
- accept: the requester is seated at the desk, the occupant is re-seated
  surgically (nobody else moves)
- decline: the requester is notified, nobody moves
- stale requests (occupant no longer holds the desk / day passed) expire and
  the requester is notified
- eligibility: you can't request a desk you're not eligible for (boss desk,
  helpdesk desks), your own desk, or a free desk
- only the occupant can decide; only the requester/occupant can view
- accepting works even after the day lock (mutual agreement)
- the map shows a "request" action on occupied desks you're eligible for
- old DBs get the new table via init_db
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

# Pre-create the OLD production schema (no password columns, no purpose, no
# hire_date_proposed, no deskrequest table) so the whole suite runs against a
# migrated DB.
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

import app.desk_requests as dr  # noqa: E402
from app.db import engine  # noqa: E402
from app.dates import upcoming_weekdays  # noqa: E402
from app.assignment import is_day_locked  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Booking, Desk, DeskRequest, Employee, MagicLink  # noqa: E402

RESULTS = []
SENT = []  # captured emails (dr.send_email is monkeypatched below)


def report(name, ok, detail):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def _capture_email(to, subject, body, **kwargs):
    SENT.append((to, subject, body))


dr.send_email = _capture_email


def unlocked_days():
    days = [d for d in upcoming_weekdays() if not is_day_locked(d)]
    if len(days) < 5:
        raise RuntimeError(f"need 5 unlocked days in window, got {len(days)}")
    return days


def get_token(email):
    with Session(engine) as s:
        return s.exec(select(MagicLink).where(MagicLink.email == email)).first().token


def login_employee(client, email, name="Emp", surname="X", team_id="1", favorite_desk_id=""):
    """Log in (or register on first use) and ensure the profile is set.
    Re-usable for the same email: after the first call the account has a
    password, so plain password login is tried first."""
    client.cookies.clear()
    r = client.post("/login", data={"email": email, "password": "test-Pass1!"})
    if r.status_code != 303:
        # No password yet (or unknown email): the magic-link flow applied.
        assert r.status_code == 200, r.text
        token = get_token(email)
        r = client.get(f"/auth/verify?token={token}")
        assert r.status_code in (302, 303, 307), r.status_code
        r = client.post("/set-password", data={"token": token, "new_password": "test-Pass1!", "confirm_password": "test-Pass1!"})
        assert r.status_code == 303, r.text
    r = client.post("/profile", data={
        "name": name, "surname": surname, "team_id": team_id,
        "favorite_desk_id": favorite_desk_id, "hire_date": "",
    })
    assert r.status_code in (302, 303, 307), r.text


def book(client, day, mode="presencial"):
    r = client.post("/calendar/book", data={"day": day.isoformat(), "mode": mode})
    assert r.status_code == 200, r.text


def desk_id(code):
    with Session(engine) as s:
        return s.exec(select(Desk).where(Desk.code == code)).first().id


def emp_id(email):
    with Session(engine) as s:
        return s.exec(select(Employee).where(Employee.email == email)).first().id


def holder_of(day, d_id):
    """Employee id assigned to desk d_id on day, or None."""
    with Session(engine) as s:
        b = s.exec(select(Booking).where(
            Booking.day == day, Booking.desk_id == d_id,
            Booking.status == "assigned",
        )).first()
        return b.employee_id if b else None


def desk_of(day, email):
    """Desk id held by `email` on day, or None."""
    with Session(engine) as s:
        b = s.exec(select(Booking).where(
            Booking.day == day, Booking.employee_id == emp_id(email),
            Booking.status == "assigned",
        )).first()
        return b.desk_id if b else None


def request_row(day, email):
    with Session(engine) as s:
        r = s.exec(select(DeskRequest).where(
            DeskRequest.day == day, DeskRequest.requester_id == emp_id(email),
        )).first()
        return (r.id, r.status, r.occupant_id, r.desk_id) if r else None


def setup_pair(days, i, prefix, occupant_favorite="", occupant_team_id="1"):
    """Book occupant and requester on days[i]; return (day, occupant_email,
    requester_email, occupant_desk_id)."""
    day = days[i]
    occ, req = f"{prefix}-occ@example.com", f"{prefix}-req@example.com"
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, occ, name=prefix.title() + "Occ", team_id=occupant_team_id,
                       favorite_desk_id=occupant_favorite)
        book(client, day)
        login_employee(client, req, name=prefix.title() + "Req")
        book(client, day)
    return day, occ, req, desk_of(day, occ)


def make_request(client, day, d_id):
    r = client.post("/map/request", data={"day": day.isoformat(), "desk_id": str(d_id)})
    return r


# --- Tests -----------------------------------------------------------------


def test_request_creates_pending_and_emails_occupant():
    days = unlocked_days()
    day, occ, req, occ_desk = setup_pair(days, 0, "rq1")
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, req)
        SENT.clear()
        r = make_request(client, day, occ_desk)
        row = request_row(day, req)
        emailed = [e for e in SENT if e[0] == occ]
        ok = (r.status_code == 303 and row is not None and row[1] == "pending"
              and row[2] == emp_id(occ) and row[3] == occ_desk
              and len(emailed) == 1 and "/requests/" in emailed[0][2])
        report("peticion crea pending + email al ocupante", ok,
               f"POST /map/request -> {r.status_code}, fila: {row}, emails al ocupante: {len(emailed)}")


def test_occupant_page_shows_accept_decline():
    days = unlocked_days()
    day, occ, req, occ_desk = setup_pair(days, 1, "rq2")
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, req)
        assert make_request(client, day, occ_desk).status_code == 303
        rid = request_row(day, req)[0]
        login_employee(client, occ)
        r = client.get(f"/requests/{rid}")
        as_occ_ok = r.status_code == 200 and "Accept" in r.text and "Decline" in r.text
        login_employee(client, req)
        r2 = client.get(f"/requests/{rid}")
        as_req_ok = r2.status_code == 200 and "Decline" not in r2.text
        report("pagina: ocupante ve Accept/Decline, peticionario ve estado",
               as_occ_ok and as_req_ok,
               f"ocupante -> {r.status_code} (botones: {as_occ_ok}), peticionario -> {r2.status_code} (sin botones: {as_req_ok})")


def test_accept_seats_requester_and_reseats_occupant():
    days = unlocked_days()
    day, occ, req, occ_desk = setup_pair(days, 2, "rq3")
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, req)
        assert make_request(client, day, occ_desk).status_code == 303
        rid = request_row(day, req)[0]
        login_employee(client, occ)
        SENT.clear()
        r = client.post(f"/requests/{rid}/decide", data={"action": "accept"})
        row = request_row(day, req)
        req_desk, occ_desk2 = desk_of(day, req), desk_of(day, occ)
        emailed = [e for e in SENT if e[0] == req]
        ok = (r.status_code == 303 and row[1] == "accepted"
              and req_desk == occ_desk and occ_desk2 is not None and occ_desk2 != occ_desk
              and len(emailed) == 1)
        report("aceptar: peticionario se sienta, ocupante reasignado", ok,
               f"decide -> {r.status_code}, estado: {row[1]}, peticionario en: {req_desk} (esperado {occ_desk}), ocupante en: {occ_desk2}, emails al peticionario: {len(emailed)}")


def test_decline_notifies_requester():
    days = unlocked_days()
    day, occ, req, occ_desk = setup_pair(days, 2, "rq4")
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, req)
        assert make_request(client, day, occ_desk).status_code == 303
        rid = request_row(day, req)[0]
        req_before = desk_of(day, req)
        login_employee(client, occ)
        SENT.clear()
        r = client.post(f"/requests/{rid}/decide", data={"action": "decline"})
        row = request_row(day, req)
        req_after = desk_of(day, req)
        emailed = [e for e in SENT if e[0] == req]
        ok = (r.status_code == 303 and row[1] == "declined"
              and req_after == req_before and req_after != occ_desk
              and len(emailed) == 1)
        report("denegar: nadie se mueve, peticionario notificado", ok,
               f"decide -> {r.status_code}, estado: {row[1]}, peticionario antes/después: {req_before}/{req_after} (esperado igual y != {occ_desk}), emails: {len(emailed)}")


def test_stale_request_expires_and_notifies():
    days = unlocked_days()
    day, occ, req, occ_desk = setup_pair(days, 3, "rq5")
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, req)
        assert make_request(client, day, occ_desk).status_code == 303
        rid = request_row(day, req)[0]
        # occupant leaves the office -> no longer holds the desk
        login_employee(client, occ)
        book(client, day, mode="teletrabajo")
        SENT.clear()
        r = client.post(f"/requests/{rid}/decide", data={"action": "accept"})
        row = request_row(day, req)
        emailed = [e for e in SENT if e[0] == req]
        ok = (r.status_code == 400 and row[1] == "expired" and len(emailed) == 1)
        report("peticion stale expira y notifica al peticionario", ok,
               f"decide -> {r.status_code} (esperado 400), estado: {row[1]}, emails al peticionario: {len(emailed)}")


def test_cannot_request_ineligible_desk():
    days = unlocked_days()
    hd01 = desk_id("HD-01")
    # occupant is a Help Desk member (team 5) with HD-01 as favorite, so the
    # desk is actually occupied by them — the 400 must come from eligibility
    day, occ, req, _ = setup_pair(days, 4, "rq6", occupant_favorite=str(hd01), occupant_team_id="5")
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, req)
        r = make_request(client, day, hd01)
        ok = r.status_code == 400 and request_row(day, req) is None
        report("no se puede pedir escritorio no elegible (helpdesk)", ok,
               f"POST /map/request HD-01 -> {r.status_code} (esperado 400), fila creada: {request_row(day, req) is not None}")


def test_cannot_request_own_desk():
    days = unlocked_days()
    day, occ, req, occ_desk = setup_pair(days, 0, "rq7")
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, occ)
        r = make_request(client, day, occ_desk)
        ok = r.status_code == 400
        report("no se puede pedir el propio escritorio", ok,
               f"POST /map/request (propio) -> {r.status_code} (esperado 400)")


def test_cannot_request_free_desk():
    days = unlocked_days()
    day, occ, req, _ = setup_pair(days, 4, "rq8")
    with Session(engine) as s:
        taken = {b.desk_id for b in s.exec(select(Booking).where(
            Booking.day == day, Booking.status == "assigned")).all()}
        free = next(d.id for d in s.exec(select(Desk)).all()
                    if d.id not in taken and d.reserved == "none")
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, req)
        r = make_request(client, day, free)
        ok = r.status_code == 400
        report("no se puede pedir escritorio libre", ok,
               f"POST /map/request (libre) -> {r.status_code} (esperado 400)")


def test_third_party_cannot_view_or_decide():
    days = unlocked_days()
    day, occ, req, occ_desk = setup_pair(days, 3, "rq9")
    stranger = "rq9-str@example.com"
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, req)
        assert make_request(client, day, occ_desk).status_code == 303
        rid = request_row(day, req)[0]
        login_employee(client, stranger, name="Stranger")
        r_view = client.get(f"/requests/{rid}")
        r_decide = client.post(f"/requests/{rid}/decide", data={"action": "accept"})
        ok = r_view.status_code == 403 and r_decide.status_code == 403
        report("tercero no ve ni decide la peticion", ok,
               f"GET -> {r_view.status_code}, POST decide -> {r_decide.status_code} (esperado 403/403)")


def test_accept_works_after_day_lock():
    days = unlocked_days()
    day, occ, req, occ_desk = setup_pair(days, 1, "rq10")
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, req)
        assert make_request(client, day, occ_desk).status_code == 303
        rid = request_row(day, req)[0]
        login_employee(client, occ)
        original = is_day_locked
        try:
            import app.assignment as assignment
            assignment.is_day_locked = lambda d, now=None: True
            r = client.post(f"/requests/{rid}/decide", data={"action": "accept"})
        finally:
            assignment.is_day_locked = original
        row = request_row(day, req)
        ok = r.status_code == 303 and row[1] == "accepted"
        report("aceptar funciona con el dia bloqueado", ok,
               f"decide con lock -> {r.status_code}, estado: {row[1]}")


def test_map_shows_request_action_on_occupied_eligible_desks():
    days = unlocked_days()
    day, occ, req, occ_desk = setup_pair(days, 0, "rq11")
    req_desk = desk_of(day, req)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_employee(client, req)
        r = client.get(f"/map?day={day.isoformat()}")
        has_occ = f'name="desk_id" value="{occ_desk}"' in r.text
        has_own = f'name="desk_id" value="{req_desk}"' in r.text
        ok = r.status_code == 200 and has_occ and not has_own
        report("mapa: enlace de peticion en escritorio ajeno, no en el propio", ok,
               f"GET /map -> {r.status_code}, enlace al escritorio del ocupante: {has_occ}, al propio: {has_own} (esperado False)")


def test_old_schema_migration():
    con = sqlite3.connect(_db_file)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    ok = "deskrequest" in tables
    report("migracion esquema antiguo (tabla nueva creada)", ok,
           f"tablas: {sorted(tables)}")


if __name__ == "__main__":
    import traceback

    for test in (
        test_request_creates_pending_and_emails_occupant,
        test_occupant_page_shows_accept_decline,
        test_accept_seats_requester_and_reseats_occupant,
        test_decline_notifies_requester,
        test_stale_request_expires_and_notifies,
        test_cannot_request_ineligible_desk,
        test_cannot_request_own_desk,
        test_cannot_request_free_desk,
        test_third_party_cannot_view_or_decide,
        test_accept_works_after_day_lock,
        test_map_shows_request_action_on_occupied_eligible_desks,
        test_old_schema_migration,
    ):
        try:
            test()
        except Exception:
            report(test.__name__, False, traceback.format_exc().strip().splitlines()[-1])
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n=== RESUMEN: {len(RESULTS) - len(failed)}/{len(RESULTS)} pasando; fallando: {failed or 'ninguno'} ===")
    sys.exit(1 if failed else 0)
