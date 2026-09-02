from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from app.admin import router as admin_router
from app.assignment import BOOKING_LOCK_HOUR, DayLockedError, book_day, is_day_locked
from app.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    current_employee,
    make_session_cookie,
    optional_employee,
    request_login,
    verify_token,
)
from app.dates import resolve_target_day, upcoming_weekdays
from app.db import get_session, init_db
from app.forms import parse_day, parse_optional_int
from app.mapview import LANDMARKS, MAP_HEIGHT, MAP_WIDTH, build_map
from app.models import Booking, Desk, Employee, Mode, Reserved, Team
from app.templating import templates

app = FastAPI(title="Hotdesk")
_static_dir = Path(__file__).resolve().parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
app.include_router(admin_router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def _desk_map(session: Session) -> dict[int, Desk]:
    return {d.id: d for d in session.exec(select(Desk)).all()}


@app.get("/", response_class=HTMLResponse)
def home(employee: Employee | None = Depends(optional_employee)):
    if not employee:
        return RedirectResponse("/login")
    if not employee.profile_complete:
        return RedirectResponse("/profile")
    return RedirectResponse("/calendar")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(...), session: Session = Depends(get_session)):
    email = email.strip().lower()
    request_login(session, email, base_url=str(request.base_url))
    return templates.TemplateResponse(request, "check_email.html", {"email": email})


@app.get("/auth/verify")
def auth_verify(token: str, session: Session = Depends(get_session)):
    employee = verify_token(session, token)
    target = "/profile" if not employee.profile_complete else "/calendar"
    response = RedirectResponse(target)
    response.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(employee.id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login")
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/profile", response_class=HTMLResponse)
def profile_form(
    request: Request,
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    desks = session.exec(select(Desk).where(Desk.reserved != Reserved.boss)).all()
    teams = session.exec(select(Team)).all()
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "employee": employee,
            "desks": desks,
            "teams": teams,
            "landmarks": LANDMARKS,
            "map_w": MAP_WIDTH,
            "map_h": MAP_HEIGHT,
        },
    )


@app.post("/profile", response_class=HTMLResponse)
def profile_submit(
    name: str = Form(...),
    surname: str = Form(...),
    team_id: str = Form(...),
    favorite_desk_id: str = Form(""),
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    team = session.get(Team, parse_optional_int(team_id) or 0)
    if not team:
        raise HTTPException(status_code=400, detail="Invalid team")

    employee.name = name.strip()
    employee.surname = surname.strip()
    employee.team_id = team.id

    desk = session.get(Desk, parse_optional_int(favorite_desk_id) or 0)
    if desk and desk.reserved == Reserved.helpdesk and not team.is_helpdesk:
        desk = None  # ignore an ineligible choice rather than erroring
    employee.favorite_desk_id = desk.id if desk else None

    session.add(employee)
    session.commit()
    return RedirectResponse("/calendar", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_view(
    request: Request,
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    days = upcoming_weekdays()
    bookings = {
        b.day: b
        for b in session.exec(
            select(Booking).where(Booking.employee_id == employee.id, Booking.day.in_(days))
        ).all()
    }
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "employee": employee,
            "days": days,
            "bookings": bookings,
            "desks": _desk_map(session),
            "lock_hour": BOOKING_LOCK_HOUR,
            "locked_days": {d for d in days if is_day_locked(d)},
        },
    )


@app.get("/map", response_class=HTMLResponse)
def map_view(
    request: Request,
    day: str | None = None,
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    days = upcoming_weekdays()
    target_day = resolve_target_day(day, days)
    desk_boxes, zone_labels, roster = build_map(session, target_day, employee)
    return templates.TemplateResponse(
        request,
        "map.html",
        {
            "desks": desk_boxes,
            "zone_labels": zone_labels,
            "roster": roster,
            "landmarks": LANDMARKS,
            "map_w": MAP_WIDTH,
            "map_h": MAP_HEIGHT,
            "day": target_day,
            "days": days,
        },
    )


@app.post("/calendar/book", response_class=HTMLResponse)
def calendar_book(
    request: Request,
    day: str = Form(...),
    mode: str = Form(...),
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    try:
        booking_mode = Mode(mode)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid mode") from None
    booking_day = parse_day(day)
    try:
        booking = book_day(session, employee, booking_day, booking_mode)
    except DayLockedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    return templates.TemplateResponse(
        request,
        "_day_row.html",
        {
            "day": booking_day,
            "booking": booking,
            "desks": _desk_map(session),
            "lock_hour": BOOKING_LOCK_HOUR,
            "locked": is_day_locked(booking_day),
        },
    )
