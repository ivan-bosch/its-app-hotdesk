from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.admin import router as admin_router
from app.assignment import (
    BOOKING_LOCK_HOUR,
    MAX_ASSIGN_RETRIES,
    DayLockedError,
    book_day,
    ensure_present_bookings,
    is_day_locked,
    resolve_day,
)
from app.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    current_employee,
    make_session_cookie,
    optional_employee,
    request_login,
    request_reset,
    set_password,
    verify_token,
)
from app.dates import resolve_target_day, upcoming_weekdays
from app.db import get_session, init_db
from app.desk_requests import router as desk_requests_router
from app.forms import parse_date, parse_day, parse_optional_int
from app.mapview import LANDMARKS, MAP_HEIGHT, MAP_WIDTH, build_map
from app.models import Booking, Desk, Employee, Mode, Reserved, Team, WorkPattern
from app.security import verify_password
from app.templating import templates

app = FastAPI(title="Hotdesk")
_static_dir = Path(__file__).resolve().parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
app.include_router(admin_router)
app.include_router(desk_requests_router)


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
        return RedirectResponse("/settings")
    return RedirectResponse("/map")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html")


def _session_response(employee: Employee, request: Request) -> RedirectResponse:
    target = "/settings" if not employee.profile_complete else "/map"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(employee.id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


def _check_email_page(request: Request, email: str):
    return templates.TemplateResponse(request, "check_email.html", {"email": email})


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(""),
    submit: str = Form("signin"),
    session: Session = Depends(get_session),
):
    email = email.strip().lower()
    # The "Forgot your password?" button shares this form: it skips the login
    # entirely and sends a reset link for the typed email (same
    # anti-enumeration behavior as the magic-link flow — unknown emails get
    # the same "check your email" page).
    if submit == "forgot":
        request_reset(session, email, base_url=str(request.base_url))
        return _check_email_page(request, email)
    employee = session.exec(select(Employee).where(Employee.email == email)).first()
    if employee and employee.password_hash:
        if not verify_password(password, employee.password_hash, employee.password_salt):
            # 200 (not 401) so the status code can't be used to map which
            # emails have a password set (user enumeration). Acceptable
            # trade-off for an internal app with known staff.
            return templates.TemplateResponse(
                request, "login.html", {"error": "Invalid email or password"}, status_code=200
            )
        return _session_response(employee, request)
    # New user, or legacy account without a password: magic link flow.
    request_login(session, email, base_url=str(request.base_url))
    return _check_email_page(request, email)


@app.post("/forgot", response_class=HTMLResponse)
def forgot_submit(request: Request, email: str = Form(...), session: Session = Depends(get_session)):
    email = email.strip().lower()
    request_reset(session, email, base_url=str(request.base_url))
    return _check_email_page(request, email)


@app.get("/auth/verify")
def auth_verify(request: Request, token: str, session: Session = Depends(get_session)):
    link, employee = verify_token(session, token, consume=False)
    if link.purpose == "reset" or not employee.password_hash:
        # First registration (or forgot password): must choose a password first.
        return RedirectResponse(f"/set-password?token={token}")
    link.used_at = datetime.utcnow()
    session.add(link)
    session.commit()
    return _session_response(employee, request)


@app.get("/set-password", response_class=HTMLResponse)
def set_password_form(request: Request, token: str, session: Session = Depends(get_session)):
    try:
        verify_token(session, token, consume=False)
    except HTTPException:
        return templates.TemplateResponse(
            request, "set_password.html", {"token": token, "error": "Invalid or expired link"}, status_code=400
        )
    return templates.TemplateResponse(request, "set_password.html", {"token": token})


@app.post("/set-password")
def set_password_submit(
    request: Request,
    token: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    session: Session = Depends(get_session),
):
    if len(new_password) < 8:
        return templates.TemplateResponse(
            request, "set_password.html", {"token": token, "error": "Password must be at least 8 characters"},
            status_code=400,
        )
    if new_password != confirm_password:
        return templates.TemplateResponse(
            request, "set_password.html", {"token": token, "error": "Passwords do not match"}, status_code=400
        )
    employee = set_password(session, token, new_password)
    return _session_response(employee, request)


@app.get("/logout")
def logout():
    response = RedirectResponse("/login")
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/profile")
def profile_redirect():
    return RedirectResponse("/settings")


@app.get("/settings", response_class=HTMLResponse)
def settings_form(
    request: Request,
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    desks = session.exec(select(Desk).where(Desk.reserved != Reserved.boss)).all()
    teams = session.exec(select(Team)).all()
    team = session.get(Team, employee.team_id or 0)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "employee": employee,
            "desks": desks,
            "teams": teams,
            "is_helpdesk": bool(team and team.is_helpdesk),
            "landmarks": LANDMARKS,
            "map_w": MAP_WIDTH,
            "map_h": MAP_HEIGHT,
        },
    )


@app.post("/profile", response_class=HTMLResponse)
@app.post("/settings", response_class=HTMLResponse)
def profile_submit(
    name: str = Form(...),
    surname: str = Form(...),
    team_id: str = Form(...),
    favorite_desk_id: str = Form(""),
    hire_date: str = Form(""),
    work_pattern: str = Form("hibrido"),
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    team = session.get(Team, parse_optional_int(team_id) or 0)
    if not team:
        raise HTTPException(status_code=400, detail="Invalid team")
    try:
        pattern = WorkPattern(work_pattern)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid work pattern") from None

    employee.name = name.strip()
    employee.surname = surname.strip()
    employee.team_id = team.id
    was_present = employee.work_pattern == WorkPattern.presencial

    # Employees only ever PROPOSE a hire date; an admin approves it before it
    # counts as seniority (self-reported seniority must not be gameable).
    proposed = parse_date(hire_date)
    employee.hire_date_proposed = proposed if proposed and proposed != employee.hire_date else None

    desk = session.get(Desk, parse_optional_int(favorite_desk_id) or 0)
    if desk and desk.reserved == Reserved.helpdesk and not team.is_helpdesk:
        desk = None  # ignore an ineligible choice rather than erroring
    employee.favorite_desk_id = desk.id if desk else None

    # Switching to in-person means remote no longer applies: future remote
    # bookings become in office and those days are re-planned (same lock-exempt
    # principle as the automatic booking — a standing declaration, not a
    # last-minute change).
    converted_days = []
    if pattern == WorkPattern.presencial and not was_present:
        remote = session.exec(
            select(Booking).where(
                Booking.employee_id == employee.id,
                Booking.day > date.today(),
                Booking.mode == Mode.teletrabajo,
            )
        ).all()
        converted_days = sorted({b.day for b in remote})
        for b in remote:
            b.mode = Mode.presencial
            session.add(b)
    employee.work_pattern = pattern

    session.add(employee)
    session.commit()
    if converted_days:
        session.expire_all()
        for day in converted_days:
            resolve_day(session, day)
    return RedirectResponse("/map", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_view(
    request: Request,
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    days = upcoming_weekdays()
    for day in days:
        ensure_present_bookings(session, day)
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
    ensure_present_bookings(session, target_day)
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
    if booking_day not in upcoming_weekdays():
        raise HTTPException(status_code=400, detail="Day is outside the booking window")
    if booking_mode == Mode.teletrabajo and employee.work_pattern == WorkPattern.presencial:
        raise HTTPException(status_code=400, detail="In-person employees can't mark remote days")
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
            "employee": employee,
            "desks": _desk_map(session),
            "lock_hour": BOOKING_LOCK_HOUR,
            "locked": is_day_locked(booking_day),
        },
    )


# --- Vacation (year-long calendar) --------------------------------------------


def _vacation_grid_context(session: Session, employee: Employee, month_first: date) -> dict:
    """Month grid for the vacation page: every day of the month with its
    editability (`past`/`weekend`/`locked` days are shown but closed) and
    whether the employee marked it as vacation."""
    today = date.today()
    month_last = (month_first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    vacations = {
        b.day
        for b in session.exec(
            select(Booking).where(
                Booking.employee_id == employee.id,
                Booking.mode == Mode.vacation,
                Booking.day >= month_first,
                Booking.day <= month_last,
            )
        ).all()
    }
    days = []
    d = month_first
    while d <= month_last:
        if d < today:
            kind = "past"
        elif d.weekday() >= 5:
            kind = "weekend"
        elif d == today and is_day_locked(d):
            kind = "locked"
        else:
            kind = "open"
        days.append({"day": d, "kind": kind, "is_vacation": d in vacations})
        d += timedelta(days=1)
    month = month_first.month
    return {
        "month_first": month_first,
        "month_label": month_first.strftime("%B %Y"),
        "days": days,
        # Month navigation clamped to the current year: January has no
        # previous month, December has no next one.
        "prev_month": f"{month_first.year:04d}-{month - 1:02d}" if month > 1 else None,
        "next_month": f"{month_first.year:04d}-{month + 1:02d}" if month < 12 else None,
        "today": today,
    }


@app.get("/vacation", response_class=HTMLResponse)
def vacation_view(
    request: Request,
    month: str = "",
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    today = date.today()
    if not month:
        month_first = today.replace(day=1)
    else:
        try:
            year, mon = (int(part) for part in month.split("-"))
            month_first = date(year, mon, 1)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid month: {month!r}") from None
    if month_first.year != today.year:
        raise HTTPException(status_code=400, detail="Only the current year can be edited")
    return templates.TemplateResponse(
        request,
        "vacation.html",
        {"employee": employee, **_vacation_grid_context(session, employee, month_first)},
    )


@app.post("/vacation/toggle", response_class=HTMLResponse)
def vacation_toggle(
    request: Request,
    day: str = Form(...),
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    d = parse_day(day)
    today = date.today()
    if d.year != today.year:
        raise HTTPException(status_code=400, detail="Only the current year can be edited")
    if d.weekday() >= 5:
        raise HTTPException(status_code=400, detail="Weekends can't be marked as vacation")
    if d < today:
        raise HTTPException(status_code=400, detail="Past days can't be changed")
    if d == today and is_day_locked(d):
        raise HTTPException(status_code=403, detail=f"{d.isoformat()} is locked (changes close at {BOOKING_LOCK_HOUR:02d}:00)")

    in_window = d in upcoming_weekdays()
    for attempt in range(MAX_ASSIGN_RETRIES):
        if attempt > 0:
            session.rollback()
        booking = session.exec(
            select(Booking).where(Booking.employee_id == employee.id, Booking.day == d)
        ).first()
        try:
            if booking is not None and booking.mode == Mode.vacation:
                # Unmarking. An in-person employee falls back to the automatic
                # in-office booking for window days.
                session.delete(booking)
                session.commit()
                if employee.work_pattern == WorkPattern.presencial and in_window:
                    ensure_present_bookings(session, d)
            else:
                # Marking. A vacation never holds a desk; only freeing an
                # existing in-office desk re-plans the day.
                freed = in_window and booking is not None and booking.mode == Mode.presencial
                if booking is None:
                    booking = Booking(employee_id=employee.id, day=d, mode=Mode.vacation)
                    session.add(booking)
                else:
                    if freed:
                        booking.desk_id = None
                        booking.status = None
                    booking.mode = Mode.vacation
                    session.add(booking)
                session.commit()
                if freed:
                    session.expire_all()
                    resolve_day(session, d)
            break
        except IntegrityError:
            continue  # a parallel toggle raced us; retry on fresh state
    else:
        session.rollback()
        raise HTTPException(status_code=503, detail="Could not save the change, please retry")

    return templates.TemplateResponse(
        request,
        "_vacation_grid.html",
        {"employee": employee, **_vacation_grid_context(session, employee, d.replace(day=1))},
    )
