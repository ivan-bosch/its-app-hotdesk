from datetime import date, datetime
from enum import Enum

from sqlmodel import Field, SQLModel, UniqueConstraint


class Reserved(str, Enum):
    none = "none"
    boss = "boss"
    helpdesk = "helpdesk"


class Mode(str, Enum):
    presencial = "presencial"
    teletrabajo = "teletrabajo"
    vacation = "vacation"


class BookingStatus(str, Enum):
    assigned = "assigned"
    waitlisted = "waitlisted"


class Team(SQLModel, table=True):
    """Employee sub-department. `is_helpdesk` marks the one team (if any) whose
    members can claim the HD-01/02/03 reserved desks (see assignment.py) —
    managed from the admin panel instead of being a hardcoded constant."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    is_helpdesk: bool = Field(default=False)


class Zone(SQLModel, table=True):
    """Display-only grouping for the map (e.g. "Entrance row", "Big block").
    Not used by the assignment algorithm — proximity is computed from each
    desk's x/y position instead (see assignment.py)."""

    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(unique=True)
    name: str


class Desk(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(unique=True)  # "A1", "HD-01", "DESP-01"
    zone_id: int = Field(foreign_key="zone.id")
    reserved: Reserved = Field(default=Reserved.none)
    x: int = 0
    y: int = 0


class Employee(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    name: str | None = None
    surname: str | None = None
    team_id: int | None = Field(default=None, foreign_key="team.id")
    favorite_desk_id: int | None = Field(default=None, foreign_key="desk.id")
    is_boss: bool = Field(default=False)
    hire_date: date | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def profile_complete(self) -> bool:
        return bool(self.name and self.surname and self.team_id)


class Booking(SQLModel, table=True):
    """Unique constraints double as the race-condition guard: SQLite rejects a
    second concurrent booking that would double-book a desk or duplicate an
    employee's booking for a day (NULLs don't collide, so many teletrabajo/
    waitlisted rows with desk_id=NULL are still fine). See assign_desk's
    retry-on-conflict in assignment.py."""

    __table_args__ = (
        UniqueConstraint("day", "desk_id"),
        UniqueConstraint("employee_id", "day"),
    )

    id: int | None = Field(default=None, primary_key=True)
    employee_id: int = Field(foreign_key="employee.id")
    day: date
    mode: Mode
    desk_id: int | None = Field(default=None, foreign_key="desk.id")
    status: BookingStatus | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MagicLink(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    token: str = Field(unique=True, index=True)
    expires_at: datetime
    used_at: datetime | None = None


class AdminUser(SQLModel, table=True):
    """Separate login from the employee magic-link system — a small number of
    admins authenticate with username/password to manage employees, teams and
    desk assignments. Password is stored as a PBKDF2 hash+salt (see admin_auth.py)."""

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    password_salt: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
