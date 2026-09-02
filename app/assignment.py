"""Desk assignment algorithm.

Instant full-recalc model, per day:

1. **Every change re-resolves the whole day.** Whenever someone marks a day as
   "in office" (or switches away from it), `resolve_day()` re-runs the full
   assignment for *everyone* who declared presencial that day, from scratch:
   boss first, then contested favorite desks go to whoever has more seniority,
   then everyone else is seated as close as possible to their team in arrival
   order, and whoever is left without an eligible desk goes to the waitlist.
   Deterministic: same set of declarations -> same assignment.

2. **Seniority displaces, never waitlists.** If a more-senior employee takes
   your contested favorite, you are reseated elsewhere (or waitlisted only if
   literally no eligible desk remains). You never lose a desk *to the
   waitlist* just because someone more senior showed up — only because the
   office is genuinely full.

3. **The day locks at `BOOKING_LOCK_HOUR`** (08:00 by default, server-local,
   on the day itself): from then on, employees can no longer change that
   day's choice (in office / remote / vacation). Future days stay open.
   Admins can still override assignments after the lock (see `admin_reassign`).

There are no fixed zones for regular teams (only Help Desk and the boss have
reserved desks) — "sit near my team" is implemented as picking the free desk
physically closest to a teammate already seated that day, using each desk's
x/y position on the map.

See ALGORITHM.md at the repo root for a full walkthrough with worked examples.
"""

import os
import threading
from datetime import date, datetime, time

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.email import send_email
from app.models import Booking, BookingStatus, Desk, Employee, Mode, Reserved, Team

BOSS_DESK_CODE = "DESP-01"
MAX_ASSIGN_RETRIES = 3

# Hour of the day (0-23, server-local time) from which a day's choice can no
# longer be changed by employees. Overridable via env var. See ALGORITHM.md.
BOOKING_LOCK_HOUR = int(os.environ.get("BOOKING_LOCK_HOUR", "8"))


# Per-day re-entrant lock serializing every same-day mutation: a booking commit
# plus its resolve_day (book_day), and an admin override plus its
# promote_waitlist (admin_reassign). Without it, two concurrent book_day calls
# for the same day run resolve_day on overlapping-but-stale snapshots and can
# double-book a desk (UNIQUE(day, desk_id) -> IntegrityError -> HTTP 500) or
# leave a booking stuck with no status. RLock (not Lock) so a high-level
# function and the resolver it calls can both hold it without deadlocking.
# Different days get different locks and never block each other.
_day_locks: dict[date, threading.RLock] = {}
_day_locks_guard = threading.Lock()


def _day_lock(day: date) -> threading.RLock:
    with _day_locks_guard:
        lock = _day_locks.get(day)
        if lock is None:
            lock = threading.RLock()
            _day_locks[day] = lock
        return lock


class DayLockedError(Exception):
    """Raised when an employee tries to change a day that is already locked
    (at/after BOOKING_LOCK_HOUR on the day itself)."""


# --- Notifications (best-effort: delivery failures never break booking) ------


def notify_waitlisted(employee: Employee, day: date) -> None:
    send_email(
        employee.email,
        "Hotdesk: no desk available",
        f"There's no free desk for you on {day.isoformat()} — you've been added to the "
        "waitlist. You'll get another email the moment a desk frees up and you're "
        "assigned one, no action needed on your part.",
        raise_on_error=False,
    )


def notify_assigned_from_waitlist(employee: Employee, day: date, desk: Desk) -> None:
    send_email(
        employee.email,
        "Hotdesk: a desk opened up for you",
        f"Good news — a desk just freed up for {day.isoformat()} and you've been "
        f"assigned desk {desk.code}. See it on the map: /map?day={day.isoformat()}",
        raise_on_error=False,
    )


# --- Day lock -----------------------------------------------------------------


def lock_datetime(day: date) -> datetime:
    """The exact moment `day` stops accepting employee changes."""
    return datetime.combine(day, time(hour=BOOKING_LOCK_HOUR))


def is_day_locked(day: date, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return now >= lock_datetime(day)


# --- Eligibility / geometry ----------------------------------------------------


def _is_helpdesk(session: Session, employee: Employee) -> bool:
    if not employee.team_id:
        return False
    team = session.get(Team, employee.team_id)
    return bool(team and team.is_helpdesk)


def _is_eligible(session: Session, desk: Desk, employee: Employee) -> bool:
    if desk.reserved == Reserved.none:
        return True
    if desk.reserved == Reserved.boss:
        return employee.is_boss
    if desk.reserved == Reserved.helpdesk:
        return _is_helpdesk(session, employee)
    return False


def _distance(a: Desk, b: Desk) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def _seniority_score(employee: Employee, today: date | None = None) -> float:
    """Years of seniority since `hire_date`, used purely as a priority
    tie-break when two employees want the same favorite desk: more years
    wins. No `hire_date` set -> 0 (loses to anyone with recorded seniority)."""
    if not employee.hire_date:
        return 0.0
    today = today or date.today()
    return max(0.0, (today - employee.hire_date).days / 365.25)


def _existing_booking(session: Session, employee: Employee, day: date) -> Booking | None:
    return session.exec(
        select(Booking).where(Booking.employee_id == employee.id, Booking.day == day)
    ).first()


# --- The assignment engine ------------------------------------------------------


def resolve_day(session: Session, day: date) -> None:
    """Re-resolves `day`, serialized against any other same-day resolution via
    `_day_lock` (see the note at the top of the module). The actual work is in
    `_resolve_day`."""
    with _day_lock(day):
        _resolve_day(session, day)


def _resolve_day(session: Session, day: date) -> None:
    """Recomputes every presencial assignment for `day` from scratch.

    Decision order, per employee declaring presencial that day:
    1. Boss -> always `DESP-01`.
    2. Favorite desk, if eligible. If 2+ employees share the same favorite,
       the one with more seniority (`_seniority_score`) wins it outright;
       the rest fall through to step 3. Ties broken by whoever declared
       first (`Booking.created_at`).
    3. Team-clustering fallback for everyone left, processed in arrival
       order: nearest free eligible desk to an already-seated teammate, then
       nearest to their own (lost) favorite as an anchor, then lowest free
       desk code.
    4. No eligible free desk left -> waitlisted.

    Anyone who landed on or left the waitlist gets a best-effort email after
    the commit. Desk-to-desk moves are silent — you see your current desk
    when you open the app. See ALGORITHM.md for a walkthrough.
    """
    presencial = session.exec(
        select(Booking).where(Booking.day == day, Booking.mode == Mode.presencial)
    ).all()
    if not presencial:
        return

    all_desks = {d.id: d for d in session.exec(select(Desk)).all()}
    employees = {b.employee_id: session.get(Employee, b.employee_id) for b in presencial}
    by_employee = {b.employee_id: b for b in presencial}
    remaining_ids = set(by_employee.keys())

    # Snapshot of previous statuses, to detect waitlist transitions and email them.
    previous_status: dict[int, BookingStatus | None] = {b.employee_id: b.status for b in presencial}

    # Free every desk first: the (day, desk_id) unique constraint would reject
    # two bookings swapping desks within one commit otherwise.
    for booking in by_employee.values():
        booking.desk_id = None
        booking.status = None
        session.add(booking)
    session.flush()

    assigned_desk_of: dict[int, int] = {}  # desk_id -> employee_id

    def is_free(desk_id: int) -> bool:
        return desk_id not in assigned_desk_of

    def eligible_free_desks(employee: Employee) -> list[Desk]:
        return [d for d in all_desks.values() if is_free(d.id) and _is_eligible(session, d, employee)]

    def place(employee_id: int, desk: Desk) -> None:
        booking = by_employee[employee_id]
        booking.desk_id = desk.id
        booking.status = BookingStatus.assigned
        assigned_desk_of[desk.id] = employee_id
        remaining_ids.discard(employee_id)

    # 1. Boss.
    for emp_id in list(remaining_ids):
        employee = employees[emp_id]
        if not employee.is_boss:
            continue
        boss_desk = next(
            (d for d in all_desks.values() if d.code == BOSS_DESK_CODE and is_free(d.id)), None
        )
        if boss_desk:
            place(emp_id, boss_desk)

    # 2. Favorite desks, contested ones resolved by seniority.
    wanters: dict[int, list[int]] = {}
    for emp_id in list(remaining_ids):
        employee = employees[emp_id]
        fav_id = employee.favorite_desk_id
        if not fav_id or not is_free(fav_id):
            continue
        fav = all_desks.get(fav_id)
        if not fav or not _is_eligible(session, fav, employee):
            continue
        wanters.setdefault(fav_id, []).append(emp_id)

    for desk_id, candidate_ids in wanters.items():
        if not is_free(desk_id):
            continue
        winner_id = max(
            candidate_ids,
            key=lambda eid: (
                _seniority_score(employees[eid]),
                -by_employee[eid].created_at.timestamp(),
                eid,  # last-resort tie-break: keep the winner deterministic
            ),
        )
        place(winner_id, all_desks[desk_id])
        # Losing candidates stay in remaining_ids for step 3.

    # 3. Team-clustering fallback, in arrival order. Must NOT sort by team_id:
    # team-less employees (team_id=None) would otherwise sort ahead of every
    # real team and grab the closest desks before actual teammates do.
    def sort_key(emp_id: int):
        return (by_employee[emp_id].created_at, emp_id)

    for emp_id in sorted(remaining_ids, key=sort_key):
        employee = employees[emp_id]
        eligible_free = [d for d in eligible_free_desks(employee) if d.code != BOSS_DESK_CODE]
        if not eligible_free:
            booking = by_employee[emp_id]
            booking.desk_id = None
            booking.status = BookingStatus.waitlisted
            continue

        teammate_desks = (
            [
                all_desks[desk_id]
                for desk_id, holder_id in assigned_desk_of.items()
                if holder_id != emp_id and employees[holder_id].team_id == employee.team_id
            ]
            if employee.team_id
            else []
        )

        if teammate_desks:
            best = min(eligible_free, key=lambda d: (min(_distance(d, t) for t in teammate_desks), d.code))
        elif employee.favorite_desk_id and employee.favorite_desk_id in all_desks:
            fav_desk = all_desks[employee.favorite_desk_id]
            best = min(eligible_free, key=lambda d: (_distance(d, fav_desk), d.code))
        else:
            best = min(eligible_free, key=lambda d: d.code)

        place(emp_id, best)

    for booking in by_employee.values():
        session.add(booking)
    session.commit()

    # Notify waitlist transitions only (entered/exited). Best-effort, after
    # the commit. Desk-to-desk moves (e.g. someone more senior took your
    # favorite) are deliberately silent — you see your current desk when you
    # open the app. A first-time assignment (no prior status) is the direct
    # result of your own click — no email either.
    for emp_id, booking in by_employee.items():
        old_status = previous_status[emp_id]
        new_status = booking.status
        if new_status == old_status:
            continue
        employee = employees[emp_id]
        if new_status == BookingStatus.waitlisted:
            notify_waitlisted(employee, day)
        elif new_status == BookingStatus.assigned and old_status == BookingStatus.waitlisted:
            notify_assigned_from_waitlist(employee, day, all_desks[booking.desk_id])


def assign_desk(session: Session, booking: Booking, employee: Employee) -> None:
    """Mutates `booking` in place (desk_id, status). Plain greedy algorithm,
    used only by `promote_waitlist` and `admin_reassign` — the normal flow
    goes through `resolve_day` (see module docstring and ALGORITHM.md)."""
    taken_ids = set(
        session.exec(
            select(Booking.desk_id).where(
                Booking.day == booking.day,
                Booking.status == BookingStatus.assigned,
                Booking.desk_id.is_not(None),
            )
        ).all()
    )
    free = [d for d in session.exec(select(Desk)).all() if d.id not in taken_ids]

    # 1. Boss always gets his desk.
    if employee.is_boss:
        boss_desk = next((d for d in free if d.code == BOSS_DESK_CODE), None)
        if boss_desk:
            booking.desk_id = boss_desk.id
            booking.status = BookingStatus.assigned
            return

    # 2. Favorite desk, if free and eligible.
    if employee.favorite_desk_id:
        fav = next((d for d in free if d.id == employee.favorite_desk_id), None)
        if fav and _is_eligible(session, fav, employee):
            booking.desk_id = fav.id
            booking.status = BookingStatus.assigned
            return

    eligible_free = [d for d in free if _is_eligible(session, d, employee) and d.reserved != Reserved.boss]
    if not eligible_free:
        booking.desk_id = None
        booking.status = BookingStatus.waitlisted
        return

    # 3. Closest free desk to a teammate already seated that day.
    teammate_desks: list[Desk] = []
    if employee.team_id:
        teammate_desks = session.exec(
            select(Desk)
            .join(Booking, Booking.desk_id == Desk.id)
            .join(Employee, Employee.id == Booking.employee_id)
            .where(
                Booking.day == booking.day,
                Booking.status == BookingStatus.assigned,
                Employee.team_id == employee.team_id,
                Employee.id != employee.id,
            )
        ).all()

    if teammate_desks:
        best = min(eligible_free, key=lambda d: (min(_distance(d, t) for t in teammate_desks), d.code))
    elif employee.favorite_desk_id:
        # Favorite is taken, but bias toward desks near it.
        fav_desk = session.get(Desk, employee.favorite_desk_id)
        best = min(eligible_free, key=lambda d: (_distance(d, fav_desk), d.code)) if fav_desk else min(
            eligible_free, key=lambda d: d.code
        )
    else:
        best = min(eligible_free, key=lambda d: d.code)

    booking.desk_id = best.id
    booking.status = BookingStatus.assigned


def promote_waitlist(session: Session, day: date) -> None:
    """Serialized same-day wrapper around `_promote_waitlist` (see `_day_lock`)."""
    with _day_lock(day):
        _promote_waitlist(session, day)


def _promote_waitlist(session: Session, day: date) -> None:
    """After an admin action frees a desk, retry every waitlisted booking in
    arrival order. (Employee-side cancellations go through `book_day`, which
    re-resolves the whole day instead — this is only for admin overrides,
    which must not reshuffle everyone else.)"""
    waiting = session.exec(
        select(Booking)
        .where(Booking.day == day, Booking.status == BookingStatus.waitlisted)
        .order_by(Booking.created_at)
    ).all()
    newly_assigned: list[tuple[Employee, Desk]] = []
    for b in waiting:
        employee = session.get(Employee, b.employee_id)
        assign_desk(session, b, employee)
        session.add(b)
        if b.status == BookingStatus.assigned and b.desk_id is not None:
            newly_assigned.append((employee, session.get(Desk, b.desk_id)))
    session.commit()

    for employee, desk in newly_assigned:
        notify_assigned_from_waitlist(employee, day, desk)


def admin_reassign(session: Session, employee: Employee, day: date, desk_id: int | None) -> Booking:
    """Serialized same-day wrapper around `_admin_reassign` (see `_day_lock`)."""
    with _day_lock(day):
        return _admin_reassign(session, employee, day, desk_id)


def _admin_reassign(session: Session, employee: Employee, day: date, desk_id: int | None) -> Booking:
    """Admin override: force `employee`'s desk for `day` to `desk_id` (or to the
    waitlist if `desk_id` is None), regardless of favorite/proximity rules and
    regardless of the employee-facing day lock. If another employee currently
    holds that desk that day, they are bumped to the waitlist and immediately
    re-evaluated by `promote_waitlist` (they may land on another free desk
    rather than staying stuck)."""
    other = None
    if desk_id is not None:
        other = session.exec(
            select(Booking).where(
                Booking.day == day,
                Booking.status == BookingStatus.assigned,
                Booking.desk_id == desk_id,
                Booking.employee_id != employee.id,
            )
        ).first()
        if other:
            other.desk_id = None
            other.status = BookingStatus.waitlisted
            session.add(other)

    booking = _existing_booking(session, employee, day) or Booking(employee_id=employee.id, day=day, mode=Mode.presencial)
    booking.mode = Mode.presencial
    booking.desk_id = desk_id
    booking.status = BookingStatus.assigned if desk_id is not None else BookingStatus.waitlisted
    session.add(booking)
    session.commit()
    promote_waitlist(session, day)
    session.refresh(booking)

    if desk_id is not None and other is not None:
        session.refresh(other)
        if other.status == BookingStatus.waitlisted:
            # promote_waitlist couldn't find another desk for them right away.
            bumped_employee = session.get(Employee, other.employee_id)
            notify_waitlisted(bumped_employee, day)

    return booking


def book_day(session: Session, employee: Employee, day: date, mode: Mode) -> Booking:
    """Serialized same-day wrapper around `_book_day` (see `_day_lock`)."""
    with _day_lock(day):
        return _book_day(session, employee, day, mode)


def _book_day(session: Session, employee: Employee, day: date, mode: Mode) -> Booking:
    """Record `employee`'s choice for `day` and, for presencial, re-resolve the
    whole day's assignments immediately (see module docstring).

    Raises `DayLockedError` if `day` is at/past its lock time — employees can
    no longer change that day (admins still can, via `admin_reassign`).

    Two people can race to book the same day. The (day, desk_id) and
    (employee_id, day) unique constraints on Booking turn that race into an
    IntegrityError on commit rather than a silent double-assignment — retry a
    bounded number of times against fresh state.

    `teletrabajo` (remote) and `vacation` are functionally identical — neither
    ever holds a desk — and share the same branch below; they're only tracked
    as distinct labels so the map view can show who's remote vs. on vacation
    vs. simply unknown."""
    if is_day_locked(day):
        raise DayLockedError(f"{day.isoformat()} is locked (changes close at {BOOKING_LOCK_HOUR:02d}:00)")

    if mode in (Mode.teletrabajo, Mode.vacation):
        existing = _existing_booking(session, employee, day)
        if existing:
            was_presencial = existing.mode == Mode.presencial
            existing.mode = mode
            existing.desk_id = None
            existing.status = None
            session.add(existing)
            session.commit()
            if was_presencial:
                resolve_day(session, day)  # freed a desk -> re-seat everyone
            return existing
        booking = Booking(employee_id=employee.id, day=day, mode=mode)
        session.add(booking)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return _existing_booking(session, employee, day)
        return booking

    # mode == presencial
    existing = _existing_booking(session, employee, day)
    if existing and existing.mode == Mode.presencial:
        return existing  # already declared for that day; nothing changes

    for attempt in range(MAX_ASSIGN_RETRIES):
        if attempt > 0:
            session.rollback()
        booking = _existing_booking(session, employee, day) or Booking(employee_id=employee.id, day=day, mode=mode)
        booking.mode = Mode.presencial
        booking.created_at = datetime.utcnow()
        session.add(booking)
        try:
            session.commit()
            break
        except IntegrityError:
            continue
    else:
        session.rollback()
        booking = _existing_booking(session, employee, day)
        if booking is None:
            raise HTTPException(status_code=503, detail="Could not record the booking, please retry")

    session.expire_all()  # resolve_day must read committed state, not stale objects
    resolve_day(session, day)
    session.refresh(booking)
    return booking
