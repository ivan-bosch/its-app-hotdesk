# Database

Hotdesk ITS stores everything in a **single SQLite file**. This document
covers the schema (all tables, columns, constraints, indexes), the seed
data, the startup migration, where the file lives in each deployment target,
and backup/restore.

---

## 1. The file and the engine

- Path comes from the `HOTDESK_DB` environment variable
  (`app/db.py::DB_PATH`), defaulting to `<repo>/hotdesk.db` (next to
  `pyproject.toml`). In Docker it is `/data/hotdesk.db`, which the compose
  file bind-mounts to `./data/` on the host.
- The engine is created with `connect_args={"check_same_thread": False}`
  because FastAPI runs synchronous routes in a threadpool and the same
  engine is used across threads.
- `SQLModel.metadata.create_all(engine)` creates any missing tables on
  startup (`init_db`, wired to FastAPI's startup event). `create_all`
  never alters existing tables, which is why a small manual migration step
  exists (see §5).
- No WAL mode, no busy-timeout tuning, no foreign-key pragma beyond what
  SQLModel emits: the app is a single process with in-process per-day
  serialization (see [ARCHITECTURE.md](ARCHITECTURE.md) §6), so default
  SQLite locking is sufficient.

---

## 2. Enums

Defined in `app/models.py`; stored as their string values.

| Enum | Values | Used by |
| --- | --- | --- |
| `Reserved` | `none`, `boss`, `helpdesk` | `Desk.reserved` |
| `Mode` | `presencial`, `teletrabajo`, `vacation` | `Booking.mode` |
| `WorkPattern` | `hibrido`, `presencial` | `Employee.work_pattern` — standing work modality (see [ALGORITHM.md](ALGORITHM.md) §In-person employees) |
| `BookingStatus` | `assigned`, `waitlisted` | `Booking.status` (nullable — see below) |
| `DeskRequestStatus` | `pending`, `accepted`, `declined`, `expired` | `DeskRequest.status` |

`Booking.status` is `NULL` for every non-presencial booking (remote,
vacation) and for a presencial booking that has not been resolved yet; it is
`assigned` or `waitlisted` once the resolver has run. The map view treats
`presencial` + `status != assigned` as waitlisted, so a stuck `NULL` is
shown rather than dropped.

---

## 3. Tables

### `team`

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | PK, autoincrement | |
| `name` | VARCHAR | UNIQUE | Display name. |
| `is_helpdesk` | BOOLEAN | default `false` | At most one team may be flagged (enforced in `admin.py`, not by a DB constraint). Its members are the only ones eligible for the `HD-01/02/03` desks. |

### `zone`

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | PK, autoincrement | |
| `code` | VARCHAR | UNIQUE | Short code (`DESP`, `SUP`, `PB`, `BLK`). |
| `name` | VARCHAR | | Display name. |

Zones are **display-only** groupings for the map. The assignment algorithm
does not use them — proximity is computed from each desk's `x`/`y`
coordinates (see [ALGORITHM.md](ALGORITHM.md)).

### `desk`

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | PK, autoincrement | |
| `code` | VARCHAR | UNIQUE | `DESP-01`, `HD-01…03`, `P01…P11`. |
| `zone_id` | INTEGER | FK → `zone.id` | |
| `reserved` | VARCHAR | default `'none'` | `Reserved` enum. `boss` → only `Employee.is_boss` may be seated there; `helpdesk` → only members of the `is_helpdesk` team. |
| `x` | INTEGER | default 0 | Map-diagram coordinate (pixels of the SVG floor plan, not meters). |
| `y` | INTEGER | default 0 | Same. |

### `employee`

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | PK, autoincrement | |
| `email` | VARCHAR | UNIQUE, indexed | Login identity; normalized to lowercase on write. |
| `name` | VARCHAR | nullable | Part of `profile_complete`. |
| `surname` | VARCHAR | nullable | Part of `profile_complete`. |
| `team_id` | INTEGER | FK → `team.id`, nullable | Part of `profile_complete`. NULL = teamless (allowed; such employees sort last in team clustering). |
| `favorite_desk_id` | INTEGER | FK → `desk.id`, nullable | At most one favorite (not a ranked list). |
| `is_boss` | BOOLEAN | default `false` | Grants exclusive access to `DESP-01`. |
| `hire_date` | DATE | nullable | **Approved** hire date. The only value the algorithm uses for seniority. |
| `hire_date_proposed` | DATE | nullable | Self-proposed hire date from `/settings`, pending admin approval. Never used by the algorithm until approved. |
| `work_pattern` | VARCHAR | default `'hibrido'` | `WorkPattern` enum. `presencial` employees are auto-booked in office every working day (no remote option); `hibrido` employees choose per day. |
| `password_hash` | VARCHAR | nullable | PBKDF2-HMAC-SHA256 hex digest. NULL = no password set yet (magic-link flow still applies). |
| `password_salt` | VARCHAR | nullable | 16-byte hex salt. |
| `created_at` | DATETIME | default `utcnow` | |

`profile_complete` is a Python property (`name and surname and team_id`),
not a column — it decides where `/` and post-login redirects point.

### `booking`

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | PK, autoincrement | |
| `employee_id` | INTEGER | FK → `employee.id` | |
| `day` | DATE | | The calendar day the booking applies to. |
| `mode` | VARCHAR | | `Mode` enum. |
| `desk_id` | INTEGER | FK → `desk.id`, nullable | NULL for remote/vacation/waitlisted. |
| `status` | VARCHAR | nullable | `BookingStatus` enum or NULL (see §2). |
| `created_at` | DATETIME | default `utcnow` | **Arrival order** — the tie-breaker used throughout the algorithm. |

**Unique constraints (the race-condition guard):**

```
UNIQUE (day, desk_id)        -- never two assigned bookings on one desk in one day
UNIQUE (employee_id, day)    -- never two bookings for one employee in one day
```

SQLite's `UNIQUE` treats `NULL`s as distinct, so any number of
remote/waitlisted rows with `desk_id = NULL` coexist fine; the constraint
only fires on a real double-booking. These constraints are the *last line
of defense* — the in-process per-day locks make the race rare, the
constraints make it harmless (see [ALGORITHM.md](ALGORITHM.md) §Race
conditions).

### `deskrequest`

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | PK, autoincrement | |
| `requester_id` | INTEGER | FK → `employee.id` | The employee asking for the desk. |
| `occupant_id` | INTEGER | FK → `employee.id` | Who held the desk when the request was made. The request is only decidable while *they* still hold it. |
| `desk_id` | INTEGER | FK → `desk.id` | |
| `day` | DATE | | The day the desk is wanted for. |
| `status` | VARCHAR | default `'pending'` | `DeskRequestStatus` enum. |
| `created_at` | DATETIME | default `utcnow` | |
| `decided_at` | DATETIME | nullable | Set on accept/decline/expire. |

No unique constraints: several employees may have pending requests for the
same desk at once; accepting one simply makes the others stale (the desk is
no longer held by the recorded occupant). Staleness is checked lazily when a
request is viewed or decided — there is no background job that expires
requests.

### `magiclink`

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | PK, autoincrement | |
| `email` | VARCHAR | indexed | Target address (not unique — several links can be outstanding). |
| `token` | VARCHAR | UNIQUE, indexed | `secrets.token_urlsafe(32)`. |
| `expires_at` | DATETIME | | 15 minutes after creation. |
| `used_at` | DATETIME | nullable | Set on consumption → single-use. |
| `purpose` | VARCHAR | default `'login'` | `'login'` (first registration) or `'reset'` (forgot password). |

### `adminuser`

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | INTEGER | PK, autoincrement | |
| `username` | VARCHAR | UNIQUE, indexed | |
| `password_hash` | VARCHAR | | PBKDF2-HMAC-SHA256 hex digest. |
| `password_salt` | VARCHAR | | 16-byte hex salt. |
| `created_at` | DATETIME | default `utcnow` | |

There is no employee↔admin linkage: admin accounts are separate from
employee accounts and do not log in through the employee flow.

---

## 4. Seed data (first run only)

`init_db` seeds each table only if it is empty:

- **Zones (4):** `DESP` "Boss office", `SUP` "Upper row", `PB` "By the
  office", `BLK` "Entrance block".
- **Desks (15),** digitized from the real ITS office floor plan (01A06):

  | Code | Zone | Reserved | (x, y) |
  | --- | --- | --- | --- |
  | `DESP-01` | DESP | boss | (90, 50) |
  | `P01` `P02` `P03` | SUP | — | (200, 50) (280, 50) (360, 50) |
  | `P09` | SUP | — | (410, 110) |
  | `P04` | SUP | — | (460, 50) |
  | `P05` `P06` | SUP | — | (560, 50) (680, 50) |
  | `P07` `P08` | PB | — | (200, 250) (280, 250) |
  | `P10` `P11` | BLK | — | (640, 200) (720, 200) |
  | `HD-01` `HD-02` `HD-03` | BLK | helpdesk | (560, 270) (640, 270) (720, 270) |

- **Teams (5):** Data, SciCom, AI, Security, Help Desk — with "Help Desk"
  flagged `is_helpdesk`.
- **Admin (1):** `admin` / `admin123`, printed to the server console once.

To change the physical desk inventory (positions, codes, adding/removing
desks) you edit `db.py::seed_desks` and start from an empty database — an
admin screen for the desk inventory is a known missing feature.

---

## 5. Startup migration (`_ensure_columns`)

SQLite cannot add columns to existing tables via `create_all`. On every
startup, `_ensure_columns` runs `PRAGMA table_info(<table>)` for a fixed list
of (table, column) pairs and issues `ALTER TABLE … ADD COLUMN` for any that
are missing:

| Table | Column | DDL |
| --- | --- | --- |
| `employee` | `password_hash` | `ALTER TABLE employee ADD COLUMN password_hash VARCHAR` |
| `employee` | `password_salt` | `ALTER TABLE employee ADD COLUMN password_salt VARCHAR` |
| `employee` | `hire_date_proposed` | `ALTER TABLE employee ADD COLUMN hire_date_proposed DATE` |
| `magiclink` | `purpose` | `ALTER TABLE magiclink ADD COLUMN purpose VARCHAR NOT NULL DEFAULT 'login'` |
| `employee` | `work_pattern` | `ALTER TABLE employee ADD COLUMN work_pattern VARCHAR NOT NULL DEFAULT 'hibrido'` |

This is how databases created by older versions of the app are upgraded in
place. The test suites deliberately pre-create a database with the *old*
schema to exercise this path on every run (see [TESTING.md](TESTING.md)).

**Adding a new column to an existing table:** add the field to the model
*and* a row to the `_ensure_columns` list. For brand-new tables,
`create_all` alone is enough.

---

## 6. Where the file lives

| Environment | `HOTDESK_DB` | File on the host |
| --- | --- | --- |
| Local dev (`uv run uvicorn …`) | default | `<repo>/hotdesk.db` (in `.gitignore`) |
| Docker (`docker compose up`) | `/data/hotdesk.db` (container path) | `<repo>/data/hotdesk.db` (bind mount `./data:/data`) |

With Docker the database is a plain file next to `docker-compose.yml`:
visible directly, and the backup is a copy of that file. To move it, change
the bind mount to an absolute path (the container-internal path
`HOTDESK_DB=/data/hotdesk.db` stays the same).

> If the directory lives on a network filesystem (NFS/Lustre), SQLite can
> misbehave with file locking under concurrency. For this app (few users,
> one writer process) it usually works; if you see "database is locked"
> errors under load, move the DB to local disk.

---

## 7. Backup and restore

The database is one file, so backup is a file copy:

```bash
# Docker: stop first so you don't copy mid-write
docker compose stop
cp data/hotdesk.db /backup/hotdesk-$(date +%F).db
docker compose start
```

Restore is the reverse: stop, copy the file back, start. There is no
separate schema file — the schema is fully determined by the models plus
`_ensure_columns`, and the seed runs automatically if tables are empty.

For a *consistent* backup while the app keeps running, use the SQLite
online backup API instead of `cp`:

```bash
sqlite3 data/hotdesk.db ".backup /backup/hotdesk.db"
```

---

## 8. Operational notes

- **One file, one writer.** Never point two app processes at the same file
  expecting the in-process locks to coordinate — they won't (see
  [ARCHITECTURE.md](ARCHITECTURE.md) §10).
- **`created_at` is UTC** (`datetime.utcnow`) while the day lock uses
  server-local time (`datetime.now()` with the container's `TZ`). This is
  intentional and harmless: `created_at` is only ever compared
  relatively (arrival order), never against wall-clock times.
- **Emails are not stored.** Outgoing mail is fire-and-forget; the only
  persistent trace of a magic link is its `magiclink` row.
