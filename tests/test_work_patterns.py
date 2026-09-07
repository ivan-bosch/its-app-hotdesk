"""Work pattern (hybrid/presencial) + vacation page tests (TDD).

Run: uv run --with httpx python tests/test_work_patterns.py

Behavior under test:
- settings form offers a work pattern select (hybrid default) and persists it
- in-person employees are auto-booked "in office" on every render of a day
  (map/calendar), get a desk, and need no daily action
- pre-marked vacation days are never overridden by the auto-booking
- switching hybrid -> in-person converts future remote bookings to in office
- in-person employees cannot book remote days (400)
- /vacation renders a month grid for the current year with month navigation
- toggling a day creates/deletes a vacation booking, even outside the
  5-day booking window (marks apply when the day reaches the window)
- weekend, past and out-of-year days are rejected
- the weekly calendar no longer offers a per-day Vacation button; in-person
  day rows show "automatic" and vacation days show "On vacation"
"""
import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_db_file = tempfile.mktemp(suffix=".db")
os.environ["HOTDESK_DB"] = _db_file
os.environ.pop("SMTP_HOST", None)

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

from app.dates import upcoming_weekdays  # noqa: E402
from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Booking, Employee, MagicLink, Mode, WorkPattern  # noqa: E402

RESULTS = []


def report(name, ok, detail):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def get_token(email):
    with Session(engine) as s:
        return s.exec(select(MagicLink).where(MagicLink.email == email)).first().token


def register(client, email, work_pattern=None):
    """Magic-link registration + password + complete profile.

    `work_pattern` defaults to hybrid (the form default)."""
    client.cookies.clear()
    r = client.post("/login", data={"email": email})
    assert r.status_code == 200, r.text
    token = get_token(email)
    r = client.get(f"/auth/verify?token={token}")
    assert r.status_code in (302, 303, 307), r.status_code
    r = client.post("/set-password", data={"token": token, "new_password": "test-Pass1!", "confirm_password": "test-Pass1!"})
    assert r.status_code == 303, r.text
    data = {"name": "Work", "surname": "Pat", "team_id": "1"}
    if work_pattern:
        data["work_pattern"] = work_pattern
    r = client.post("/profile", data=data)
    assert r.status_code in (302, 303, 307), r.text


def booking_for(email, day):
    with Session(engine) as s:
        emp = s.exec(select(Employee).where(Employee.email == email)).first()
        return s.exec(select(Booking).where(Booking.employee_id == emp.id, Booking.day == day)).first()


def _future_out_of_window_weekday():
    """A weekday of the current year that is not in the 5-day booking window."""
    today = date.today()
    window = set(upcoming_weekdays())
    d = today + timedelta(days=1)
    while d.year == today.year:
        if d.weekday() < 5 and d not in window:
            return d
        d += timedelta(days=1)
    raise AssertionError("no out-of-window weekday left this year")


def test_settings_shows_work_pattern():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp1@example.com")
        r = client.get("/settings")
        has_select = 'name="work_pattern"' in r.text
        has_default = '<option value="hibrido" selected' in r.text
        report("settings muestra work pattern (híbrido por defecto)",
               r.status_code == 200 and has_select and has_default,
               f"GET /settings -> {r.status_code}, select: {has_select}, default híbrido: {has_default}")


def test_work_pattern_persists():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp2@example.com")
        r = client.post("/settings", data={"name": "W", "surname": "P", "team_id": "1", "work_pattern": "presencial"})
        with Session(engine) as s:
            emp = s.exec(select(Employee).where(Employee.email == "wp2@example.com")).first()
        ok = r.status_code == 303 and emp.work_pattern == WorkPattern.presencial
        report("work_pattern=presencial persiste", ok, f"POST /settings -> {r.status_code}, guardado: {emp.work_pattern}")


def test_present_employee_auto_booked_on_map():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp3@example.com", work_pattern="presencial")
        day = upcoming_weekdays()[0]
        client.get(f"/map?day={day.isoformat()}")
        b = booking_for("wp3@example.com", day)
        ok = b is not None and b.mode == Mode.presencial
        report("presencial auto-marcado al ver el mapa", ok,
               f"booking {day}: {b.mode if b else None}")


def test_present_employee_auto_booked_gets_desk():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp4@example.com", work_pattern="presencial")
        day = upcoming_weekdays()[0]
        client.get(f"/map?day={day.isoformat()}")
        b = booking_for("wp4@example.com", day)
        ok = b is not None and b.desk_id is not None and b.status is not None and b.status.value == "assigned"
        report("presencial auto-marcado con escritorio", ok,
               f"desk_id={getattr(b, 'desk_id', None)}, status={getattr(b.status, 'value', None)}")


def test_present_employee_vacation_not_overridden():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp5@example.com", work_pattern="presencial")
        day = upcoming_weekdays()[-1]
        r = client.post("/vacation/toggle", data={"day": day.isoformat()})
        assert r.status_code == 200, r.text
        client.get(f"/map?day={day.isoformat()}")
        b = booking_for("wp5@example.com", day)
        ok = b is not None and b.mode == Mode.vacation
        report("vacación marcada no la pisa el auto-marking", ok,
               f"booking {day}: {b.mode if b else None}")


def test_hybrid_to_present_converts_remote():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp6@example.com")
        day = upcoming_weekdays()[-1]
        r = client.post("/calendar/book", data={"day": day.isoformat(), "mode": "teletrabajo"})
        assert r.status_code == 200, r.text
        r = client.post("/settings", data={"name": "W", "surname": "P", "team_id": "1", "work_pattern": "presencial"})
        assert r.status_code == 303, r.text
        b = booking_for("wp6@example.com", day)
        ok = b is not None and b.mode == Mode.presencial and b.desk_id is not None
        report("híbrido->presencial convierte el remote a in office", ok,
               f"booking {day}: mode={b.mode if b else None}, desk={getattr(b, 'desk_id', None)}")


def test_remote_blocked_for_present():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp7@example.com", work_pattern="presencial")
        day = upcoming_weekdays()[0]
        r = client.post("/calendar/book", data={"day": day.isoformat(), "mode": "teletrabajo"})
        ok = r.status_code == 400
        report("remote bloqueado para presencial (400)", ok, f"POST /calendar/book teletrabajo -> {r.status_code}")


def test_vacation_page_renders_month_grid():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp8@example.com")
        r = client.get("/vacation")
        month_name = date.today().strftime("%B")
        ok = (r.status_code == 200 and month_name in r.text and 'id="vacation-grid"' in r.text)
        report("/vacation renderiza la cuadrícula del mes", ok,
               f"GET /vacation -> {r.status_code}, mes '{month_name}': {month_name in r.text}")


def test_vacation_toggle_creates_out_of_window():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp9@example.com")
        day = _future_out_of_window_weekday()
        r = client.post("/vacation/toggle", data={"day": day.isoformat()})
        b = booking_for("wp9@example.com", day)
        ok = r.status_code == 200 and b is not None and b.mode == Mode.vacation
        report("toggle crea vacación fuera de la ventana", ok,
               f"{day}: {r.status_code}, booking: {b.mode if b else None}")


def test_vacation_toggle_off_deletes():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp10@example.com")
        day = _future_out_of_window_weekday()
        client.post("/vacation/toggle", data={"day": day.isoformat()})
        r = client.post("/vacation/toggle", data={"day": day.isoformat()})
        b = booking_for("wp10@example.com", day)
        ok = r.status_code == 200 and b is None
        report("toggle off borra la vacación", ok, f"{day}: {r.status_code}, booking restante: {b}")


def test_vacation_toggle_rejects_weekend_and_past():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp11@example.com")
        today = date.today()
        w = today + timedelta(days=1)
        while w.weekday() < 5:
            w += timedelta(days=1)
        p = today - timedelta(days=1)
        while p.weekday() >= 5:
            p -= timedelta(days=1)
        r_weekend = client.post("/vacation/toggle", data={"day": w.isoformat()})
        r_past = client.post("/vacation/toggle", data={"day": p.isoformat()})
        ok = r_weekend.status_code == 400 and r_past.status_code == 400
        report("toggle rechaza fin de semana y pasado", ok,
               f"weekend {w}: {r_weekend.status_code}, pasado {p}: {r_past.status_code}")


def test_calendar_has_no_vacation_button():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp12@example.com")
        r = client.get("/calendar")
        no_vacation = '"mode": "vacation"' not in r.text
        has_presencial = '"mode": "presencial"' in r.text
        has_remote = '"mode": "teletrabajo"' in r.text
        report("calendario sin botón Vacation (In office/Remote quedan)",
               r.status_code == 200 and no_vacation and has_presencial and has_remote,
               f"sin vacation: {no_vacation}, presencial: {has_presencial}, remote: {has_remote}")


def test_present_day_row_automatic():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp13@example.com", work_pattern="presencial")
        r = client.get("/calendar")
        ok = (r.status_code == 200 and "automatic" in r.text.lower()
              and '/calendar/book' not in r.text)
        report("fila presencial: 'automatic' y sin botones", ok,
               f"'automatic': {'automatic' in r.text.lower()}, sin botones: {'/calendar/book' not in r.text}")


def test_calendar_shows_on_vacation():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp14@example.com")
        day = upcoming_weekdays()[-1]
        client.post("/vacation/toggle", data={"day": day.isoformat()})
        r = client.get("/calendar")
        ok = r.status_code == 200 and "On vacation" in r.text
        report("calendario muestra 'On vacation' en el día marcado", ok,
               f"'On vacation': {'On vacation' in r.text}")


def test_vacation_month_out_of_year_rejected():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "wp15@example.com")
        year = date.today().year
        r_out = client.get(f"/vacation?month={year + 1}-01")
        r_in = client.get(f"/vacation?month={year}-01")
        ok = r_out.status_code == 400 and r_in.status_code == 200
        report("mes fuera del año -> 400, mes del año -> 200", ok,
               f"{year + 1}-01: {r_out.status_code}, {year}-01: {r_in.status_code}")


if __name__ == "__main__":
    import traceback

    for test in (
        test_settings_shows_work_pattern,
        test_work_pattern_persists,
        test_present_employee_auto_booked_on_map,
        test_present_employee_auto_booked_gets_desk,
        test_present_employee_vacation_not_overridden,
        test_hybrid_to_present_converts_remote,
        test_remote_blocked_for_present,
        test_vacation_page_renders_month_grid,
        test_vacation_toggle_creates_out_of_window,
        test_vacation_toggle_off_deletes,
        test_vacation_toggle_rejects_weekend_and_past,
        test_calendar_has_no_vacation_button,
        test_present_day_row_automatic,
        test_calendar_shows_on_vacation,
        test_vacation_month_out_of_year_rejected,
    ):
        try:
            test()
        except Exception:
            report(test.__name__, False, traceback.format_exc().strip().splitlines()[-1])
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n=== RESUMEN: {len(RESULTS) - len(failed)}/{len(RESULTS)} pasando; fallando: {failed or 'ninguno'} ===")
    sys.exit(1 if failed else 0)
