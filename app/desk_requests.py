"""Desk requests ("request this desk" from the map).

An employee who wants a specific occupied desk asks its current holder to
cede it. The occupant gets an email with a link to a decision page; accepting
seats the requester at the desk (via admin_reassign, which also re-seats the
occupant surgically) and declining just notifies the requester. A request
that goes stale — the desk changed hands, the day passed — is marked expired
lazily (when viewed or decided) and the requester is notified.

Accepting deliberately bypasses the day lock: a cede is a mutual agreement
between two people who are both affected, the same semantics as an admin
override.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.assignment import _is_eligible, admin_reassign
from app.auth import current_employee
from app.dates import upcoming_weekdays
from app.db import get_session
from app.email import send_email
from app.forms import parse_day, parse_optional_int
from app.models import Booking, BookingStatus, Desk, DeskRequest, DeskRequestStatus, Employee
from app.templating import templates

router = APIRouter()


def _holder_booking(session: Session, day, desk_id: int) -> Booking | None:
    return session.exec(
        select(Booking).where(
            Booking.day == day,
            Booking.desk_id == desk_id,
            Booking.status == BookingStatus.assigned,
        )
    ).first()


def _request_state(session: Session, req: DeskRequest) -> tuple[bool, str]:
    """Why a pending request can (not) be decided right now."""
    if req.status != DeskRequestStatus.pending:
        return False, "already decided"
    if req.day not in upcoming_weekdays():
        return False, "the day is no longer in the booking window"
    holder = _holder_booking(session, req.day, req.desk_id)
    if holder is None:
        return False, "the desk is now free"
    if holder.employee_id != req.occupant_id:
        return False, "the desk is now held by someone else"
    return True, ""


def _expire(session: Session, req: DeskRequest, reason: str) -> None:
    req.status = DeskRequestStatus.expired
    req.decided_at = datetime.utcnow()
    session.add(req)
    session.commit()
    requester = session.get(Employee, req.requester_id)
    desk = session.get(Desk, req.desk_id)
    send_email(
        requester.email,
        "Hotdesk: your desk request is no longer valid",
        f"Your request for desk {desk.code} on {req.day.isoformat()} is no longer "
        f"valid ({reason}). If the desk is free you can simply declare the day as "
        "in office from your calendar.",
        raise_on_error=False,
    )


@router.post("/map/request")
def map_request(
    request: Request,
    day: str = Form(...),
    desk_id: str = Form(...),
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    booking_day = parse_day(day)
    if booking_day not in upcoming_weekdays():
        raise HTTPException(status_code=400, detail="Day is outside the booking window")
    desk = session.get(Desk, parse_optional_int(desk_id) or 0)
    if not desk:
        raise HTTPException(status_code=404, detail="Desk not found")
    holder = _holder_booking(session, booking_day, desk.id)
    if not holder:
        raise HTTPException(status_code=400, detail="That desk is not occupied on that day")
    occupant = session.get(Employee, holder.employee_id)
    if occupant.id == employee.id:
        raise HTTPException(status_code=400, detail="That is your own desk")
    if not _is_eligible(session, desk, employee):
        raise HTTPException(status_code=400, detail="You are not eligible for that desk")

    req = DeskRequest(
        requester_id=employee.id,
        occupant_id=occupant.id,
        desk_id=desk.id,
        day=booking_day,
    )
    session.add(req)
    session.commit()
    session.refresh(req)

    base = str(request.base_url).rstrip("/")
    full_name = f"{employee.name or ''} {employee.surname or ''}".strip() or employee.email
    send_email(
        occupant.email,
        f"Hotdesk: {full_name} is requesting desk {desk.code}",
        f"{full_name} ({employee.email}) is requesting desk {desk.code} for "
        f"{booking_day.isoformat()}. If you want to cede it, accept or decline here: "
        f"{base}/requests/{req.id}",
        raise_on_error=False,
    )
    return RedirectResponse(f"/map?day={booking_day.isoformat()}", status_code=303)


@router.get("/requests/{request_id}")
def request_view(
    request: Request,
    request_id: int,
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    req = session.get(DeskRequest, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if employee.id not in (req.requester_id, req.occupant_id):
        raise HTTPException(status_code=403, detail="Not your request")

    valid, reason = _request_state(session, req)
    if not valid and req.status == DeskRequestStatus.pending:
        _expire(session, req, reason)
        session.refresh(req)

    requester = session.get(Employee, req.requester_id)
    occupant = session.get(Employee, req.occupant_id)
    desk = session.get(Desk, req.desk_id)
    return templates.TemplateResponse(
        request,
        "request.html",
        {
            "employee": employee,
            "req": req,
            "requester": requester,
            "occupant": occupant,
            "desk": desk,
            "viewer_is_occupant": employee.id == req.occupant_id,
            "valid": valid,
            "reason": reason,
        },
    )


@router.post("/requests/{request_id}/decide")
def request_decide(
    request_id: int,
    action: str = Form(...),
    employee: Employee = Depends(current_employee),
    session: Session = Depends(get_session),
):
    req = session.get(DeskRequest, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if employee.id != req.occupant_id:
        raise HTTPException(status_code=403, detail="Only the occupant can decide")

    valid, reason = _request_state(session, req)
    if not valid:
        if req.status == DeskRequestStatus.pending:
            _expire(session, req, reason)
        raise HTTPException(status_code=400, detail="This request is no longer valid")

    requester = session.get(Employee, req.requester_id)
    desk = session.get(Desk, req.desk_id)
    if action == "accept":
        # Seats the requester at the desk and re-seats the occupant
        # surgically (nobody else moves). Bypasses the day lock on purpose.
        admin_reassign(session, requester, req.day, req.desk_id)
        req.status = DeskRequestStatus.accepted
        req.decided_at = datetime.utcnow()
        session.add(req)
        session.commit()
        send_email(
            requester.email,
            f"Hotdesk: desk {desk.code} ceded to you",
            f"Good news — the holder of desk {desk.code} accepted your request for "
            f"{req.day.isoformat()}. You're seated there now: "
            f"/map?day={req.day.isoformat()}",
            raise_on_error=False,
        )
    else:
        req.status = DeskRequestStatus.declined
        req.decided_at = datetime.utcnow()
        session.add(req)
        session.commit()
        send_email(
            requester.email,
            f"Hotdesk: your request for desk {desk.code} was declined",
            f"The holder of desk {desk.code} declined your request for "
            f"{req.day.isoformat()}.",
            raise_on_error=False,
        )
    return RedirectResponse(f"/map?day={req.day.isoformat()}", status_code=303)
