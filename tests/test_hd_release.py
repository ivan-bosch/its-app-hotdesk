"""Help Desk desk release tests (TDD).

An HD desk is claimed by the Help Desk employees who favor it. A claimed
desk stays reserved for the Help Desk team until every claimant has marked
the day as vacation or remote (an undeclared claimant keeps it reserved —
they might still show up). Desks no Help Desk employee favors are in the
general pool all the time. Released desks are open to everyone in the
seating logic and show as free (not reserved) on the map.

Run: uv run --with httpx python tests/test_hd_release.py
"""
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_db_file = tempfile.mktemp(suffix=".db")
os.environ["HOTDESK_DB"] = _db_file
os.environ.pop("SMTP_HOST", None)

# Pre-create the OLD production schema (no password columns, no purpose, no
# work_pattern, ...) so the whole suite runs against a migrated DB.
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

PASSWORD = "test-Pass1!"
HD_STAFF = {"maria": "HD-01", "juan": "HD-02", "carlos": "HD-03"}

RESULTS = []


def report(name, ok, detail):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def unlocked_days():
    days = [d for d in upcoming_weekdays() if not is_day_locked(d)]
    if len(days) < 4:
        raise RuntimeError("need 4 unlocked days in the booking window")
    return days


def get_token(email):
    with Session(engine) as s:
        return s.exec(select(MagicLink).where(MagicLink.email == email)).first().token


def desk_id(code):
    with Session(engine) as s:
        return s.exec(select(Desk).where(Desk.code == code)).first().id


def register(client, email, name, surname, team_id, favorite_desk_id=""):
    client.cookies.clear()
    r = client.post("/login", data={"email": email})
    assert r.status_code == 200, r.text
    token = get_token(email)
    r = client.get(f"/auth/verify?token={token}")
    assert r.status_code in (302, 303, 307), r.text
    r = client.post(
        "/set-password",
        data={"token": token, "new_password": PASSWORD, "confirm_password": PASSWORD},
    )
    assert r.status_code == 303, r.text
    r = client.post(
        "/settings",
        data={
            "name": name,
            "surname": surname,
            "team_id": team_id,
            "favorite_desk_id": favorite_desk_id,
            "work_pattern": "hibrido",
        },
    )
    assert r.status_code in (302, 303), r.text


def login_back(client, email):
    client.cookies.clear()
    r = client.post("/login", data={"email": email, "password": PASSWORD})
    assert r.status_code in (302, 303), r.text


def book(client, day, mode="presencial"):
    r = client.post("/calendar/book", data={"day": day.isoformat(), "mode": mode})
    assert r.status_code == 200, r.text


def bookings_on(day):
    """email -> (mode, status, desk_code) for every booking on `day`."""
    with Session(engine) as s:
        rows = s.exec(
            select(Booking, Employee)
            .join(Employee, Employee.id == Booking.employee_id)
            .where(Booking.day == day)
        ).all()
        return {
            e.email: (
                b.mode.value,
                b.status.value if b.status else None,
                s.get(Desk, b.desk_id).code if b.desk_id else None,
            )
            for b, e in rows
        }


def map_rect_class(client, day, code):
    r = client.get(f"/map?day={day.isoformat()}")
    assert r.status_code == 200, r.text
    m = re.search(
        r'<rect class="(desk-\w+)"[^>]*>\s*<text class="desk-label"[^>]*>' + re.escape(code) + r"</text>",
        r.text,
    )
    assert m, f"desk {code} not found on map"
    return m.group(1)


def non_hd(desks):
    return {e: v for e, v in desks.items() if e.startswith("n")}


def test_setup_people():
    days = unlocked_days()
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        for who, code in HD_STAFF.items():
            register(client, f"{who}@hd.example", who.title(), "H", "5", str(desk_id(code)))
        for i in range(1, 13):
            register(client, f"n{i:02d}@off.example", f"N{i}", "O", "1")
    report("setup: 15 personas registradas (3 HD + 12 oficina)", True,
           f"dias desbloqueados: {[d.isoformat() for d in days]}")


def test_vacation_releases_favorite_hd():
    d0 = unlocked_days()[0]
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_back(client, "maria@hd.example")
        book(client, d0)
        login_back(client, "juan@hd.example")
        book(client, d0)
        login_back(client, "carlos@hd.example")
        book(client, d0, "vacation")
        for i in range(1, 13):
            login_back(client, f"n{i:02d}@off.example")
            book(client, d0)
        b = bookings_on(d0)
        nh = non_hd(b)
        waitlisted = [e for e, v in nh.items() if v[1] == "waitlisted"]
        on_hd03 = [e for e, v in nh.items() if v[2] == "HD-03"]
        on_p = [e for e, v in nh.items() if (v[2] or "").startswith("P")]
        ok = (
            b["maria@hd.example"][2] == "HD-01"
            and b["juan@hd.example"][2] == "HD-02"
            and not waitlisted
            and len(on_hd03) == 1
            and len(on_p) == 11
        )
        report("vacacion libera el HD del que la marca",
               ok,
               f"maria={b['maria@hd.example'][2]}, juan={b['juan@hd.example'][2]}, "
               f"waitlist={waitlisted}, HD-03={on_hd03}, P={len(on_p)}")


def test_remote_releases_favorite_hd():
    d0 = unlocked_days()[0]
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_back(client, "carlos@hd.example")
        book(client, d0, "teletrabajo")
        b = bookings_on(d0)
        nh = non_hd(b)
        waitlisted = [e for e, v in nh.items() if v[1] == "waitlisted"]
        on_hd03 = [e for e, v in nh.items() if v[2] == "HD-03"]
        ok = b["carlos@hd.example"][0] == "teletrabajo" and not waitlisted and len(on_hd03) == 1
        report("remote tambien libera el HD del que lo marca",
               ok,
               f"carlos={b['carlos@hd.example']}, waitlist={waitlisted}, HD-03={on_hd03}")


def test_unclaimed_hds_join_the_pool():
    d1 = unlocked_days()[1]
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_back(client, "maria@hd.example")
        book(client, d1)
        login_back(client, "juan@hd.example")
        book(client, d1, "vacation")
        login_back(client, "carlos@hd.example")
        book(client, d1, "vacation")
        for i in range(1, 13):
            login_back(client, f"n{i:02d}@off.example")
            book(client, d1)
        b = bookings_on(d1)
        nh = non_hd(b)
        waitlisted = [e for e, v in nh.items() if v[1] == "waitlisted"]
        on_hd = [e for e, v in nh.items() if v[2] in ("HD-02", "HD-03")]
        on_desp = [e for e, v in nh.items() if v[2] == "DESP-01"]
        ok = b["maria@hd.example"][2] == "HD-01" and not waitlisted and len(on_hd) >= 1 and not on_desp
        report("HD sin titular presente entran en el pool",
               ok,
               f"maria={b['maria@hd.example'][2]}, waitlist={waitlisted}, no-HD en HD={on_hd}")


def test_map_shows_released_hd_as_free():
    d2 = unlocked_days()[2]
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_back(client, "maria@hd.example")
        book(client, d2)
        # juan no declara (su HD-02 sigue reservado); carlos de vacaciones (HD-03 libre)
        login_back(client, "carlos@hd.example")
        book(client, d2, "vacation")
        login_back(client, "n01@off.example")
        classes = {code: map_rect_class(client, d2, code) for code in ("HD-01", "HD-02", "HD-03", "P01")}
        ok = (
            classes["HD-01"] == "desk-occupied"
            and classes["HD-02"] == "desk-reserved"
            and classes["HD-03"] == "desk-free"
            and classes["P01"] == "desk-free"
        )
        report("mapa: HD liberado se ve libre, HD sin declarar sigue punteado", ok, str(classes))


def test_undeclared_claimant_keeps_hd_reserved():
    d3 = unlocked_days()[3]
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        for i in range(1, 13):
            login_back(client, f"n{i:02d}@off.example")
            book(client, d3)
        b = bookings_on(d3)
        nh = non_hd(b)
        waitlisted = [e for e, v in nh.items() if v[1] == "waitlisted"]
        on_p = [e for e, v in nh.items() if (v[2] or "").startswith("P")]
        on_hd = [e for e, v in nh.items() if v[2] in ("HD-01", "HD-02", "HD-03")]
        ok = len(waitlisted) == 1 and len(on_p) == 11 and not on_hd
        report("HD sin declarar sigue reservado (puede llegar)",
               ok,
               f"waitlist={waitlisted}, P={len(on_p)}, no-HD en HD={on_hd}")


def test_in_office_claimant_sits_and_keeps_reserved():
    d3 = unlocked_days()[3]
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_back(client, "juan@hd.example")
        book(client, d3)
        b = bookings_on(d3)
        nh = non_hd(b)
        waitlisted = [e for e, v in nh.items() if v[1] == "waitlisted"]
        on_hd = [e for e, v in nh.items() if v[2] in ("HD-01", "HD-02", "HD-03")]
        ok = b["juan@hd.example"][2] == "HD-02" and len(waitlisted) == 1 and not on_hd
        report("HD que declara in-office se sienta y sigue reservado",
               ok,
               f"juan={b['juan@hd.example'][2]}, waitlist={waitlisted}, no-HD en HD={on_hd}")


def test_remote_claimant_releases_and_waitlist_promoted():
    d3 = unlocked_days()[3]
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_back(client, "juan@hd.example")
        book(client, d3, "teletrabajo")
        b = bookings_on(d3)
        nh = non_hd(b)
        waitlisted = [e for e, v in nh.items() if v[1] == "waitlisted"]
        on_hd02 = [e for e, v in nh.items() if v[2] == "HD-02"]
        on_p = [e for e, v in nh.items() if (v[2] or "").startswith("P")]
        ok = not waitlisted and len(on_hd02) == 1 and len(on_p) == 11
        report("HD pasa a remote -> el waitlist sube a su HD",
               ok,
               f"juan={b['juan@hd.example'][0]}, waitlist={waitlisted}, HD-02={on_hd02}, P={len(on_p)}")


def test_settings_locks_hds_for_non_helpdesk():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        locked_g = r'<g class="[^"]*pf-locked[^"]*"\s+data-id="\d+" data-code="{}"'
        login_back(client, "n01@off.example")
        r = client.get("/settings")
        assert r.status_code == 200
        hd01_locked = re.search(locked_g.format("HD-01"), r.text)
        p01_locked = re.search(locked_g.format("P01"), r.text)
        login_back(client, "maria@hd.example")
        r2 = client.get("/settings")
        assert r2.status_code == 200
        hd_page_locked = re.search(r'<g class="[^"]*pf-locked', r2.text)
        ok = bool(hd01_locked) and not p01_locked and not hd_page_locked
        report("settings: HD no clicable para no-HD, normal para HD",
               ok,
               f"no-HD HD-01 bloqueado={bool(hd01_locked)}, no-HD P01 bloqueado={bool(p01_locked)}, "
               f"pagina HD con pf-locked={hd_page_locked}")


def test_non_helpdesk_cannot_favorite_an_hdd():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        login_back(client, "n02@off.example")
        r = client.post(
            "/settings",
            data={
                "name": "N2",
                "surname": "O",
                "team_id": "1",
                "favorite_desk_id": str(desk_id("HD-01")),
                "work_pattern": "hibrido",
            },
        )
        assert r.status_code in (302, 303), r.text
        with Session(engine) as s:
            e = s.exec(select(Employee).where(Employee.email == "n02@off.example")).first()
            s.refresh(e)
            ok = e.favorite_desk_id is None
        report("no-HD no puede poner un HD de favorito (el servidor lo descarta)", ok,
               f"favorite={e.favorite_desk_id}")


if __name__ == "__main__":
    import traceback

    for test in (
        test_setup_people,
        test_vacation_releases_favorite_hd,
        test_remote_releases_favorite_hd,
        test_unclaimed_hds_join_the_pool,
        test_map_shows_released_hd_as_free,
        test_undeclared_claimant_keeps_hd_reserved,
        test_in_office_claimant_sits_and_keeps_reserved,
        test_remote_claimant_releases_and_waitlist_promoted,
        test_settings_locks_hds_for_non_helpdesk,
        test_non_helpdesk_cannot_favorite_an_hdd,
    ):
        try:
            test()
        except Exception:
            report(test.__name__, False, traceback.format_exc().strip().splitlines()[-1])
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n=== RESUMEN: {len(RESULTS) - len(failed)}/{len(RESULTS)} pasando; fallando: {failed or 'ninguno'} ===")
    sys.exit(1 if failed else 0)
