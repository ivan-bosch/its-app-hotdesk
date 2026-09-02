"""Admin panel: username/password login, separate from the employee magic-link
system. Lets an administrator manage employees, teams, and manually reassign an
employee's desk for a given day (see assignment.py::admin_reassign)."""

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlmodel import Session, select

from app.admin_auth import (
    ADMIN_SESSION_COOKIE,
    ADMIN_SESSION_MAX_AGE,
    authenticate_admin,
    current_admin,
    make_admin_session_cookie,
)
from app.assignment import admin_reassign
from app.dates import resolve_target_day, upcoming_weekdays
from app.db import get_session
from app.forms import parse_day, parse_optional_int
from app.models import AdminUser, Booking, BookingStatus, Desk, Employee, MagicLink, Mode, Team
from app.security import hash_password, verify_password
from app.templating import templates

router = APIRouter(prefix="/admin")


# --- Auth ---------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def admin_login_form(request: Request):
    return templates.TemplateResponse(request, "admin_login.html", {})


@router.post("/login", response_class=HTMLResponse)
def admin_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    admin = authenticate_admin(session, username.strip(), password)
    if not admin:
        return templates.TemplateResponse(
            request, "admin_login.html", {"error": "Invalid username or password"}, status_code=401
        )
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        make_admin_session_cookie(admin.id),
        max_age=ADMIN_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
def admin_logout():
    response = RedirectResponse("/admin/login")
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return response


# --- Dashboard ------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    today = date.today()
    today_bookings = session.exec(select(Booking).where(Booking.day == today)).all()
    stats = {
        "assigned": sum(1 for b in today_bookings if b.status == BookingStatus.assigned),
        "waitlisted": sum(1 for b in today_bookings if b.status == BookingStatus.waitlisted),
        "remote": sum(1 for b in today_bookings if b.mode == Mode.teletrabajo),
        "vacation": sum(1 for b in today_bookings if b.mode == Mode.vacation),
    }
    return templates.TemplateResponse(
        request,
        "admin_dashboard.html",
        {
            "admin": admin,
            "employee_count": len(session.exec(select(Employee)).all()),
            "team_count": len(session.exec(select(Team)).all()),
            "today": today,
            "stats": stats,
        },
    )


# --- Employees --------------------------------------------------------------

_PRESENCIAL_LABELS = {
    BookingStatus.assigned: "Assigned",
    BookingStatus.waitlisted: "Waitlisted",
}


def _booking_status_label(booking: Booking | None) -> str:
    if booking is None:
        return "No booking"
    if booking.mode == Mode.teletrabajo:
        return "Remote"
    if booking.mode == Mode.vacation:
        return "Vacation"
    return _PRESENCIAL_LABELS.get(booking.status, "—")


def _apply_employee_form(
    employee: Employee,
    name: str,
    surname: str,
    team_id: str,
    is_boss: bool,
    hire_date: str,
) -> None:
    """Shared field parsing for the add- and edit-employee endpoints."""
    employee.name = name.strip() or None
    employee.surname = surname.strip() or None
    employee.team_id = parse_optional_int(team_id)
    employee.is_boss = is_boss
    employee.hire_date = date.fromisoformat(hire_date) if hire_date else None


@router.get("/employees", response_class=HTMLResponse)
def admin_employees(
    request: Request,
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    employees = session.exec(select(Employee)).all()
    teams = session.exec(select(Team)).all()
    return templates.TemplateResponse(
        request,
        "admin_employees.html",
        {"admin": admin, "employees": employees, "teams": teams, "teams_by_id": {t.id: t for t in teams}},
    )


@router.post("/employees", response_class=HTMLResponse)
def admin_employees_add(
    email: str = Form(...),
    name: str = Form(""),
    surname: str = Form(""),
    team_id: str = Form(""),
    is_boss: bool = Form(False),
    hire_date: str = Form(""),
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    email = email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    if session.exec(select(Employee).where(Employee.email == email)).first():
        raise HTTPException(status_code=400, detail="An employee with that email already exists")

    employee = Employee(email=email)
    _apply_employee_form(employee, name, surname, team_id, is_boss, hire_date)
    session.add(employee)
    session.commit()
    return RedirectResponse("/admin/employees", status_code=303)


@router.get("/employees/{employee_id}/edit", response_class=HTMLResponse)
def admin_employees_edit_form(
    request: Request,
    employee_id: int,
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    employee = session.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    teams = session.exec(select(Team)).all()
    return templates.TemplateResponse(
        request, "admin_employee_edit.html", {"admin": admin, "employee": employee, "teams": teams}
    )


@router.post("/employees/{employee_id}/edit", response_class=HTMLResponse)
def admin_employees_edit_submit(
    employee_id: int,
    name: str = Form(""),
    surname: str = Form(""),
    team_id: str = Form(""),
    is_boss: bool = Form(False),
    hire_date: str = Form(""),
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    employee = session.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    _apply_employee_form(employee, name, surname, team_id, is_boss, hire_date)
    session.add(employee)
    session.commit()
    return RedirectResponse("/admin/employees", status_code=303)


@router.post("/employees/{employee_id}/delete", response_class=HTMLResponse)
def admin_employees_delete(
    employee_id: int,
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    employee = session.get(Employee, employee_id)
    if employee:
        for booking in session.exec(select(Booking).where(Booking.employee_id == employee_id)).all():
            session.delete(booking)
        for link in session.exec(select(MagicLink).where(MagicLink.email == employee.email)).all():
            session.delete(link)
        session.delete(employee)
        session.commit()
    return RedirectResponse("/admin/employees", status_code=303)


# --- Teams ------------------------------------------------------------------


@router.get("/teams", response_class=HTMLResponse)
def admin_teams(
    request: Request,
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    teams = session.exec(select(Team)).all()
    # One GROUP BY query instead of one COUNT per team.
    counts = dict(
        session.exec(
            select(Employee.team_id, func.count()).group_by(Employee.team_id)
        ).all()
    )
    return templates.TemplateResponse(
        request, "admin_teams.html", {"admin": admin, "teams": teams, "counts": counts}
    )


@router.post("/teams", response_class=HTMLResponse)
def admin_teams_add(
    name: str = Form(...),
    is_helpdesk: bool = Form(False),
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Team name is required")
    if session.exec(select(Team).where(Team.name == name)).first():
        raise HTTPException(status_code=400, detail="A team with that name already exists")
    session.add(Team(name=name, is_helpdesk=is_helpdesk))
    session.commit()
    return RedirectResponse("/admin/teams", status_code=303)


@router.post("/teams/{team_id}/delete", response_class=HTMLResponse)
def admin_teams_delete(
    team_id: int,
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    team = session.get(Team, team_id)
    if team:
        # Unassign rather than block  employees keep their account,deletion 
        # they just need to pick a new team from their profile.
        for employee in session.exec(select(Employee).where(Employee.team_id == team_id)).all():
            employee.team_id = None
            session.add(employee)
        session.delete(team)
        session.commit()
    return RedirectResponse("/admin/teams", status_code=303)


# --- Reassign desks ---------------------------------------------------------


@router.get("/reassign", response_class=HTMLResponse)
def admin_reassign_form(
    request: Request,
    day: str | None = None,
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    days = upcoming_weekdays()
    target_day = resolve_target_day(day, days)

    employees = session.exec(select(Employee)).all()
    bookings = {b.employee_id: b for b in session.exec(select(Booking).where(Booking.day == target_day)).all()}
    desks = session.exec(select(Desk)).all()
    taken_desk_ids = {
        b.desk_id for b in bookings.values() if b.status == BookingStatus.assigned and b.desk_id is not None
    }

    rows = []
    for employee in employees:
        booking = bookings.get(employee.id)
        current_desk_id = booking.desk_id if booking and booking.status == BookingStatus.assigned else None
        rows.append(
            {
                "employee": employee,
                "status_label": _booking_status_label(booking),
                "current_desk_id": current_desk_id,
                "available_desks": [d for d in desks if d.id == current_desk_id or d.id not in taken_desk_ids],
            }
        )

    return templates.TemplateResponse(
        request,
        "admin_reassign.html",
        {"admin": admin, "days": days, "day": target_day, "rows": rows},
    )


@router.post("/reassign", response_class=HTMLResponse)
def admin_reassign_submit(
    day: str = Form(...),
    employee_id: int = Form(...),
    desk_id: str = Form(""),
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    employee = session.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    target_day = parse_day(day)
    admin_reassign(session, employee, target_day, parse_optional_int(desk_id))
    return RedirectResponse(f"/admin/reassign?day={day}", status_code=303)


# --- Password ---------------------------------------------------------------


def _password_error(request: Request, admin: AdminUser, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "admin_password.html", {"admin": admin, "error": message}, status_code=400
    )


@router.get("/password", response_class=HTMLResponse)
def admin_password_form(
    request: Request,
    admin: AdminUser = Depends(current_admin),
):
    return templates.TemplateResponse(request, "admin_password.html", {"admin": admin})


@router.post("/password", response_class=HTMLResponse)
def admin_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    admin: AdminUser = Depends(current_admin),
    session: Session = Depends(get_session),
):
    if not verify_password(current_password, admin.password_hash, admin.password_salt):
        return _password_error(request, admin, "Current password is incorrect")
    if len(new_password) < 8:
        return _password_error(request, admin, "New password must be at least 8 characters")
    if new_password != confirm_password:
        return _password_error(request, admin, "Passwords do not match")

    admin.password_hash, admin.password_salt = hash_password(new_password)
    session.add(admin)
    session.commit()
    return templates.TemplateResponse(request, "admin_password.html", {"admin": admin, "success": "Password updated"})
