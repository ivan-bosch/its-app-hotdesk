import os
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from app.models import AdminUser, Desk, Reserved, Team, Zone
from app.security import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME, hash_password

DB_PATH = Path(os.getenv("HOTDESK_DB", str(Path(__file__).resolve().parent.parent / "hotdesk.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

DEFAULT_TEAMS = ["Data", "SciCom", "AI", "Security", "Help Desk"]
HELPDESK_TEAM_NAME = "Help Desk"


def get_session():
    with Session(engine) as session:
        yield session


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        if not session.exec(select(Zone)).first():
            seed_desks(session)
        if not session.exec(select(Team)).first():
            seed_teams(session)
        if not session.exec(select(AdminUser)).first():
            seed_admin(session)


def seed_teams(session: Session) -> None:
    session.add_all(Team(name=name, is_helpdesk=(name == HELPDESK_TEAM_NAME)) for name in DEFAULT_TEAMS)
    session.commit()


def seed_admin(session: Session) -> None:
    password_hash, salt = hash_password(DEFAULT_ADMIN_PASSWORD)
    session.add(AdminUser(username=DEFAULT_ADMIN_USERNAME, password_hash=password_hash, password_salt=salt))
    session.commit()
    print(
        f"\n--- ADMIN ACCOUNT CREATED ---\n"
        f"username: {DEFAULT_ADMIN_USERNAME}\n"
        f"password: {DEFAULT_ADMIN_PASSWORD}\n"
        f"Change this password from /admin/password after logging in.\n---\n"
    )


def seed_desks(session: Session) -> None:
    """Digitized from the real floor plan (ITS office 01A06). 14 desks + the
    boss's desk; only DESP-01 and HD-01/02/03 are reserved — everything else
    is one open pool, clustered by physical distance (see assignment.py)."""
    zones = [
        Zone(code="DESP", name="Boss office"),
        Zone(code="SUP", name="Upper row"),
        Zone(code="PB", name="By the office"),
        Zone(code="BLK", name="Entrance block"),
    ]
    session.add_all(zones)
    session.commit()
    by_code = {z.code: z.id for z in zones}

    desks = [
        Desk(code="DESP-01", zone_id=by_code["DESP"], reserved=Reserved.boss, x=90, y=50),
        # Upper row: a pod of 3 + a striped desk + 2 loose desks near the 2nd pillar
        Desk(code="P01", zone_id=by_code["SUP"], x=200, y=50),
        Desk(code="P02", zone_id=by_code["SUP"], x=280, y=50),
        Desk(code="P03", zone_id=by_code["SUP"], x=360, y=50),
        Desk(code="P09", zone_id=by_code["SUP"], x=410, y=110),  # between P03 and P04, the striped desk next door
        Desk(code="P04", zone_id=by_code["SUP"], x=460, y=50),  # striped desk
        Desk(code="P05", zone_id=by_code["SUP"], x=560, y=50),
        Desk(code="P06", zone_id=by_code["SUP"], x=680, y=50),
        # By the office, facing into the room
        Desk(code="P07", zone_id=by_code["PB"], x=200, y=250),
        Desk(code="P08", zone_id=by_code["PB"], x=280, y=250),
        # Entrance block: open front row (2) + Help Desk back row (3)
        Desk(code="P10", zone_id=by_code["BLK"], x=640, y=200),
        Desk(code="P11", zone_id=by_code["BLK"], x=720, y=200),
        Desk(code="HD-01", zone_id=by_code["BLK"], reserved=Reserved.helpdesk, x=560, y=270),
        Desk(code="HD-02", zone_id=by_code["BLK"], reserved=Reserved.helpdesk, x=640, y=270),
        Desk(code="HD-03", zone_id=by_code["BLK"], reserved=Reserved.helpdesk, x=720, y=270),
    ]
    session.add_all(desks)
    session.commit()
