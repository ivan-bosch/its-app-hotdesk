"""Home/settings + map palette tests (TDD).

Run: uv run --with httpx python tests/test_home_settings.py

Behavior under test:
- "/" goes to the office map (today) when the profile is complete,
  and to /settings (first-time setup) when it is not
- /profile redirects to /settings (profile editing lives in settings)
- /settings renders the profile form + logout
- employee pages share a nav (Map / Calendar / Settings / Log out)
- the map page offers a "Plan your week" action to the calendar
- the office map uses a fixed light "paper" palette (theme-independent)
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
from app.models import MagicLink  # noqa: E402

RESULTS = []


def report(name, ok, detail):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def get_token(email):
    with Session(engine) as s:
        return s.exec(select(MagicLink).where(MagicLink.email == email)).first().token


def register(client, email, complete=True):
    """Magic-link registration + password; optionally fill the profile."""
    client.cookies.clear()
    r = client.post("/login", data={"email": email})
    assert r.status_code == 200, r.text
    token = get_token(email)
    r = client.get(f"/auth/verify?token={token}")
    assert r.status_code in (302, 303, 307), r.status_code
    r = client.post("/set-password", data={"token": token, "new_password": "test-Pass1!", "confirm_password": "test-Pass1!"})
    assert r.status_code == 303, r.text
    if complete:
        r = client.post("/profile", data={"name": "Home", "surname": "User", "team_id": "1"})
        assert r.status_code in (302, 303, 307), r.text


def test_home_goes_to_map_when_profile_complete():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "home-full@example.com", complete=True)
        r = client.get("/")
        report("home -> /map con perfil completo", r.status_code in (302, 303, 307) and r.headers.get("location", "").startswith("/map"),
               f"GET / -> {r.status_code} {r.headers.get('location', '')!r}")


def test_home_goes_to_settings_when_profile_incomplete():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "home-new@example.com", complete=False)
        r = client.get("/")
        report("home -> /settings con perfil incompleto", r.status_code in (302, 303, 307) and r.headers.get("location", "").startswith("/settings"),
               f"GET / -> {r.status_code} {r.headers.get('location', '')!r}")


def test_profile_redirects_to_settings():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "prof@example.com")
        r = client.get("/profile")
        report("/profile -> /settings", r.status_code in (302, 303, 307) and r.headers.get("location", "").startswith("/settings"),
               f"GET /profile -> {r.status_code} {r.headers.get('location', '')!r}")


def test_settings_page_renders_form_and_logout():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "set@example.com")
        r = client.get("/settings")
        form_ok = 'action="/settings"' in r.text
        logout_ok = "/logout" in r.text
        ok = r.status_code == 200 and form_ok and logout_ok
        report("/settings muestra formulario + logout", ok,
               f"GET /settings -> {r.status_code}, formulario: {form_ok}, logout: {logout_ok}")


def test_nav_on_employee_pages():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "nav@example.com")
        for path in ("/map", "/calendar"):
            r = client.get(path)
            ok = r.status_code == 200 and all(f'href="{h}"' in r.text for h in ("/map", "/calendar", "/settings"))
            report(f"nav en {path}", ok,
                   f"links /map,/calendar,/settings presentes: {ok}")


def test_map_has_plan_your_week():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "plan@example.com")
        r = client.get("/map")
        ok = r.status_code == 200 and "Plan your week" in r.text and 'href="/calendar"' in r.text
        report("mapa ofrece 'Plan your week' -> /calendar", ok, f"presente: {ok}")


def test_settings_post_primary_target():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "spost@example.com", complete=False)
        r = client.post("/settings", data={"name": "S", "surname": "P", "team_id": "1"})
        ok = r.status_code in (302, 303, 307) and r.headers.get("location", "").startswith("/map")
        report("POST /settings (target primario del formulario)", ok,
               f"POST /settings -> {r.status_code} {r.headers.get('location', '')!r}")


def test_map_fixed_light_palette():
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        register(client, "pal@example.com")
        r = client.get("/map")
        has_paper = "#f8f7f4" in r.text
        no_pico_vars = "var(--pico-muted-border-color)" not in r.text
        report("mapa con paleta clara fija", r.status_code == 200 and has_paper and no_pico_vars,
               f"fondo papel #f8f7f4: {has_paper}, sin vars pico en el mapa: {no_pico_vars}")


if __name__ == "__main__":
    import traceback

    for test in (
        test_home_goes_to_map_when_profile_complete,
        test_home_goes_to_settings_when_profile_incomplete,
        test_profile_redirects_to_settings,
        test_settings_page_renders_form_and_logout,
        test_nav_on_employee_pages,
        test_map_has_plan_your_week,
        test_settings_post_primary_target,
        test_map_fixed_light_palette,
    ):
        try:
            test()
        except Exception:
            report(test.__name__, False, traceback.format_exc().strip().splitlines()[-1])
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n=== RESUMEN: {len(RESULTS) - len(failed)}/{len(RESULTS)} pasando; fallando: {failed or 'ninguno'} ===")
    sys.exit(1 if failed else 0)
