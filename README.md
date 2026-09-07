# Hotdesk ITS

Internal hot-desking app for the ITS office (IRB Barcelona). Hybrid setup:
there are fewer physical desks than employees, so everyone marks day by day
whether they're coming into the office or working remotely, and the app
**automatically assigns them a desk instantly** — favoring their favorite
spot and being close to their team. Every change re-plans the whole day from
scratch; there is no batch job and no pending state.

Two standing patterns on top of the day-by-day choice: employees whose work
pattern is **in person** (Help Desk, interns, …) are booked in automatically
every working day — no daily action, no remote option — and everyone manages
**vacation days** on a year-long calendar that applies the marks automatically
when the day arrives.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, request flow, module map, process/threading model, security model, error handling, configuration. |
| [docs/API.md](docs/API.md) | Complete route reference: every endpoint, its parameters, behavior, and status codes. |
| [docs/DATABASE.md](docs/DATABASE.md) | Full schema (7 tables), seed data, startup migration, backup/restore. |
| [docs/ALGORITHM.md](docs/ALGORITHM.md) | The desk assignment algorithm: decision procedure, pseudocode, worked examples, concurrency, invariants. |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Local development, SMTP configuration, Docker, systemd, operations, security checklist, troubleshooting. |
| [docs/HTTPS.md](docs/HTTPS.md) | Serving over HTTPS: Caddy, nginx + certbot, intranet/self-signed. |
| [docs/TESTING.md](docs/TESTING.md) | The test suites, harness design, how to add tests. |

## Stack

FastAPI + SQLModel (SQLite) + Jinja2 + HTMX + Pico.css. All-Python backend,
no JS/CSS build — chosen this way because a single person maintains it and
the app needs to open well from a phone without depending on a frontend
pipeline.

- **Employee auth**: first login is a magic link by email, which forces the
  user to choose a password (`/set-password`); afterwards login is
  email+password at `/login`, and "forgot password" sends a reset magic link
  that sets a new password.
- **Admin auth**: separate username/password login at `/admin/login` for
  managing employees, teams, and desk assignments.
- **Email**: real SMTP via `app/email.py::send_email`, configured with
  environment variables (Gmail, Office365, or an institutional relay). If
  `SMTP_HOST` is not set, links are printed to the server console, so login
  works end-to-end in development without any mail server.
- **Database**: SQLite, created and seeded automatically on first start
  (desks from the real floor plan, default teams, default admin account).
- **Assignment**: instant, with full recalculation on every change — see
  [docs/ALGORITHM.md](docs/ALGORITHM.md).
- **Desk requests**: from the map, an employee can "request this desk" on an
  occupied desk; the occupant gets an email with a link to accept or decline.
  Accepting seats the requester there and re-seats the occupant.
- **Work pattern** (`/settings`): hybrid (choose per day) or in person
  (auto-booked in office every working day, no remote option).
- **Help Desk desks** (`HD-01/02/03`): reserved for the Help Desk team —
  only the team can favorite them. When the team member who favors an HD
  desk marks a day as vacation or remote, that desk is **released to the
  general pool** for that day (it shows as free on the map and the
  waitlist can be promoted onto it); the desk is theirs again as soon as
  they're back.
- **Vacation** (`/vacation`): a month-grid calendar for the current year;
  tapping a day saves the mark immediately, even months ahead.

## Quickstart

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000`. On first run a default admin account is
created and printed once to the console (`admin` / `admin123`) — log in at
`/admin/login` and change the password immediately from `/admin/password`.

Docker:

```bash
cp .env.example .env        # set SECRET_KEY (and SMTP_* for real email)
mkdir -p data && chown 10001:10001 data
docker compose up --build -d
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full guide (SMTP setup,
database location, migrations, operations) and
[docs/HTTPS.md](docs/HTTPS.md) for serving over TLS.

## Configuration

All configuration is via environment variables (no config file):

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | `dev-secret-change-me` | Signs session cookies (employee and admin). **Must** be set to a real secret before deploying. |
| `HOTDESK_DB` | `<repo>/hotdesk.db` | Path of the SQLite file (`/data/hotdesk.db` in Docker). |
| `SMTP_HOST` | unset (console fallback) | SMTP server hostname. Setting this enables real email sending. |
| `SMTP_PORT` | `587` | SMTP server port. |
| `SMTP_USER` | unset | SMTP auth username (leave unset for an open/no-auth relay). |
| `SMTP_PASSWORD` | unset | SMTP auth password. |
| `SMTP_FROM` | `SMTP_USER` or `hotdesk@localhost` | `From:` address on outgoing emails. |
| `SMTP_USE_TLS` | `true` | Whether to call `STARTTLS` before sending. |
| `BOOKING_LOCK_HOUR` | `8` | Server-local hour (0–23) from which a day's choice can no longer be changed by employees (admins can still override). |

## Code structure

```
app/
  models.py        SQLModel tables (Employee, Desk, Zone, Team, Booking,
                   MagicLink, AdminUser) + enums
  db.py            SQLite engine, initial seed (desks digitized from the real
                   floor plan, default teams, default admin), column migration
  security.py      Password hashing (PBKDF2) + SECRET_KEY
  email.py         SMTP config + send_email (console fallback when unconfigured)
  auth.py          Employee magic link (registration/reset), password
                   set/verify, signed session cookie, dependencies
  admin_auth.py    Admin username/password login, separate signed session
                   cookie, dependencies
  admin.py         Admin panel routes (employees, teams, desk reassignment,
                   password change)
  assignment.py    The desk assignment algorithm — the heart of the app
  desk_requests.py "Request this desk" flow: employee asks the occupant to
                   cede a desk; accept/decline via emailed link
  mapview.py       Turns a day's booking state into data for the SVG map
  dates.py         "Upcoming weekdays" + day-picker helpers
  forms.py         Form/query parsing helpers (dates, optional ints)
  templating.py    The single shared Jinja2Templates instance
  main.py          FastAPI routes (login, settings, calendar, map)
  templates/       Jinja2 + HTMX; the office SVG map lives in
                   _office_landmarks.html (shared by map.html and
                   settings.html); admin_*.html are the admin panel pages
  static/style.css The single app stylesheet (brand color, cards, nav,
                   mobile table/day-picker scrolling) — served at /static,
                   no build step
tests/             End-to-end suites (see docs/TESTING.md)
docs/              This documentation
```

The landing page (`/`) is the office map for today; the calendar is reached
from the "Plan your week" button; vacation days live under `/vacation`.
Profile editing — including the work pattern (hybrid / in person) — lives
under `/settings` (`/profile` still redirects there). The map uses a fixed
light "paper" palette so it stays legible in both light and dark OS themes.

No frontend build or JS framework: the only real interactivity (toggle
in-office/remote without reloading, marking vacation days, clicking the map
to pick a favorite) is HTMX plus a bit of vanilla JS in `settings.html`.

## Admin panel (`/admin`)

A separate login (username + password, its own `admin_session` cookie) lets
an administrator:

- **Employees** (`/admin/employees`) — add, edit (including hire date, the
  seniority weight), or delete an employee (also removes their bookings and
  pending magic links). Employees can propose their own hire date from
  `/settings`; it shows up here with **Approve / Reject** buttons and only
  counts as seniority once approved (self-reported seniority must not be
  gameable).
- **Teams** (`/admin/teams`) — teams are a database table, not a fixed
  list: add, or delete a team (its employees are left without a team rather
  than blocking the deletion). Exactly one team can be flagged
  `is_helpdesk`, which makes its members eligible for the `HD-01/02/03`
  reserved desks (a released one — its claimant away that day — joins the
  general pool; see [docs/ALGORITHM.md](docs/ALGORITHM.md) §Released Help
  Desk desks).
- **Reassign desks** (`/admin/reassign`) — for a chosen day, force any
  employee's desk to a specific one (or the waitlist), regardless of
  favorite/proximity rules and the day lock. The previous holder is bumped
  to the waitlist and immediately re-evaluated.
- **Password** (`/admin/password`) — change the admin password.

## What's missing

- Admin screen to edit the physical desk/zone inventory without touching
  `db.py::seed_desks` (positions, adding/removing desks).
- Decide where it's hosted (not decided yet — the stack was chosen to be
  portable).
