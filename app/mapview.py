"""Builds the occupancy data for the office map view.

Desk positions come straight from the DB (Desk.x/Desk.y, seeded in db.py from
the real floor plan). A few non-bookable landmarks (the boss's round table,
pillars, the main door) are hardcoded here purely for visual context — they
match the coordinate scheme used when seeding desks.
"""

from dataclasses import dataclass, field
from datetime import date

from sqlmodel import Session, select

from app.models import Booking, BookingStatus, Desk, Employee, Mode, Reserved, Zone

DESK_W, DESK_H = 60, 38

LANDMARKS = {
    "boss_office": {"x": 10, "y": 10, "w": 165, "h": 320},
    "round_table": {"cx": 65, "cy": 240, "r": 34},
    "pillar_1": {"x": 130, "y": 150, "w": 16, "h": 16},
    "pillar_2": {"x": 545, "y": 140, "w": 16, "h": 16},
    "main_door": {"x": 440, "y": 330},
}

MAP_WIDTH, MAP_HEIGHT = 800, 340


@dataclass
class DeskBox:
    code: str
    x: int
    y: int
    w: int = DESK_W
    h: int = DESK_H
    status: str = "free"  # free | mine | occupied | reserved-empty
    occupant: str | None = None


@dataclass
class ZoneLabel:
    name: str
    x: int
    y: int


@dataclass
class DayRoster:
    """Who's doing what on a given day, for everyone who isn't shown as an
    occupied desk box on the map — used to answer "who's remote today?",
    "who's on vacation?", and "who hasn't said anything yet?" at a glance."""

    remote: list[Employee] = field(default_factory=list)
    vacation: list[Employee] = field(default_factory=list)
    waitlisted: list[Employee] = field(default_factory=list)
    unknown: list[Employee] = field(default_factory=list)

    def sections(self) -> list[tuple[str, list[Employee]]]:
        """(title, members) pairs in display order, so the template is a
        single loop instead of one near-identical block per bucket."""
        return [
            ("Remote", self.remote),
            ("On vacation", self.vacation),
            ("Waitlisted, no desk", self.waitlisted),
            ("Unknown", self.unknown),
        ]


def build_map(
    session: Session, day: date, viewer: Employee
) -> tuple[list[DeskBox], list[ZoneLabel], DayRoster]:
    desks = session.exec(select(Desk)).all()
    zones = {z.id: z for z in session.exec(select(Zone)).all()}

    all_bookings = session.exec(select(Booking).where(Booking.day == day)).all()
    booking_by_employee = {b.employee_id: b for b in all_bookings}

    occupant_by_desk: dict[int, Employee] = {}
    rows = session.exec(
        select(Booking, Employee)
        .join(Employee, Employee.id == Booking.employee_id)
        .where(Booking.day == day, Booking.status == BookingStatus.assigned)
    ).all()
    for booking, employee in rows:
        occupant_by_desk[booking.desk_id] = employee

    desk_boxes: list[DeskBox] = []
    zone_desks: dict[int, list[Desk]] = {}
    for d in desks:
        occupant = occupant_by_desk.get(d.id)
        if occupant and occupant.id == viewer.id:
            status = "mine"
        elif occupant:
            status = "occupied"
        elif d.reserved != Reserved.none:
            status = "reserved-empty"
        else:
            status = "free"

        desk_boxes.append(
            DeskBox(
                code=d.code,
                x=d.x,
                y=d.y,
                status=status,
                occupant=f"{occupant.name} {occupant.surname}" if occupant else None,
            )
        )
        zone_desks.setdefault(d.zone_id, []).append(d)

    zone_labels = [
        ZoneLabel(name=zones[zid].name, x=min(d.x for d in members), y=min(d.y for d in members) - 16)
        for zid, members in zone_desks.items()
        if zid in zones
    ]

    # Everyone who isn't sitting at a desk box above: remote, on vacation,
    # waitlisted (declared in-office but no desk available), or simply
    # hasn't declared anything for this day at all.
    employees = session.exec(select(Employee)).all()
    roster = DayRoster()
    for employee in employees:
        booking = booking_by_employee.get(employee.id)
        if booking is None:
            roster.unknown.append(employee)
        elif booking.mode == Mode.teletrabajo:
            roster.remote.append(employee)
        elif booking.mode == Mode.vacation:
            roster.vacation.append(employee)
        elif booking.mode == Mode.presencial and booking.status != BookingStatus.assigned:
            # waitlisted — or status=None, the inconsistent state left behind
            # if a resolve_day run was interrupted; show it, don't drop it.
            roster.waitlisted.append(employee)
        # presencial + assigned is already represented by the desk boxes above.

    return desk_boxes, zone_labels, roster
