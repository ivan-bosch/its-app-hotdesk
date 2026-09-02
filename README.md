# Hotdesk ITS

Internal hot-desking app for the ITS office (IRB Barcelona), hybrid setup: there
are fewer physical desks than employees, so everyone marks day by day whether
they're coming into the office or working remotely, and the app automatically
assigns them a desk, favoring being close to their team and their preferred
spot.

## Stack

FastAPI + SQLModel (SQLite) + Jinja2 + HTMX + Pico.css. All-Python backend, no
JS/CSS build — chosen this way because a single person maintains it and the
app needs to open well from a phone without depending on a frontend pipeline.

- **Employee auth**: magic link by email (no passwords).
- **Admin auth**: separate username/password login at `/admin/login` for
  managing employees, teams, and desk assignments (see below).
- **Email**: real SMTP sending via `app/auth.py::send_email`, configured with
  environment variables (works with Gmail, Office365, or an institutional mail
  relay — see "Environment variables" below). If `SMTP_HOST` is not set, it
  falls back to printing the link to the server console, so login still works
  end-to-end in local development without any mail server.
- **Database**: SQLite (`hotdesk.db`, in `.gitignore`). Created and seeded
  automatically the first time the app starts (`app/db.py::init_db`) — desks
  from the real floor plan, default teams, and a default admin account.
- **Assignment**: desks are assigned instantly, and the whole day is
  re-planned on every change — see
  [The assignment algorithm](#the-assignment-algorithm-appassignmentpy) below.

## How to run it

```bash
uv run uvicorn app.main:app --reload --port 8000
```

or using the preview configured in `.claude/launch.json`. Open
`http://localhost:8000`.

On first run, a default admin account is created and printed once to the
console:

```
username: admin
password: admin123
```

Log in at `/admin/login` and change the password immediately from
`/admin/password`.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | `dev-secret-change-me` | Signs session cookies (employee and admin). **Must** be set to a real secret before deploying. |
| `SMTP_HOST` | unset (console fallback) | SMTP server hostname. Setting this enables real email sending. |
| `SMTP_PORT` | `587` | SMTP server port. |
| `SMTP_USER` | unset | SMTP auth username (leave unset for an open/no-auth relay). |
| `SMTP_PASSWORD` | unset | SMTP auth password. |
| `SMTP_FROM` | `SMTP_USER` or `hotdesk@localhost` | `From:` address on outgoing emails. |
| `SMTP_USE_TLS` | `true` | Whether to call `STARTTLS` before sending. |
| `BOOKING_LOCK_HOUR` | `8` | Server-local hour (0-23) from which a day's choice can no longer be changed by employees (admins can still override). See [the assignment algorithm](#the-assignment-algorithm-appassignmentpy). |

## Code structure

```
app/
  models.py        SQLModel tables (Employee, Desk, Zone, Team, Booking,
                    MagicLink, AdminUser)
  db.py            SQLite engine + initial seed (desks digitized from the real
                    floor plan, default teams, default admin account)
  security.py      Password hashing (PBKDF2) + SECRET_KEY, shared by db.py,
                    auth.py and admin_auth.py
  email.py         SMTP config + send_email (console fallback when unconfigured)
  auth.py          Employee magic link, signed session cookie, current_employee
                    / optional_employee dependencies
  admin_auth.py    Admin username/password login, separate signed session
                    cookie, current_admin / optional_admin dependencies
  admin.py         Admin panel routes (employees, teams, desk reassignment,
                    password change) — see below
  assignment.py    The desk assignment algorithm (see below) — the heart of
                    the app; also `admin_reassign` for manual admin overrides
  mapview.py       Turns a day's booking state into data ready to draw the SVG
                    map
  dates.py         Shared "upcoming weekdays" + day-picker helpers used by
                    main.py and admin.py
  forms.py         Form/query parsing helpers (dates, optional ints)
  templating.py    The single shared Jinja2Templates instance
  main.py          FastAPI routes (login, profile, calendar, map)
  templates/       Jinja2 + HTMX; the office SVG map (desk/pillar layout) lives
                    in _office_landmarks.html, included by both map.html
                    (view occupancy) and profile.html (pick a favorite by
                    clicking); admin_*.html are the admin panel pages
```

No frontend build or JS framework: the only real interactivity (toggle
in-office/remote without reloading, clicking the map to pick a favorite) is
HTMX plus a bit of vanilla JS in `profile.html`.

## Admin panel (`/admin`)

A separate login (username + password, its own `admin_session` cookie) lets an
administrator:

- **Employees** (`/admin/employees`) — add an employee record directly (email,
  name, team, boss flag, optional hire date), edit an existing one (including
  hire date, used as a seniority priority weight — see the algorithm below),
  or delete one (also removes their bookings and any pending magic links).
- **Teams** (`/admin/teams`) — teams are a database table (`Team`), not a fixed
  list: add, or delete a team (its employees are left without a team rather
  than blocking the deletion). Exactly one team can be flagged
  `is_helpdesk`, which is what makes its members eligible for the
  `HD-01/02/03` reserved desks (see the algorithm below) — no more hardcoded
  `HELPDESK_TEAM` constant.
- **Reassign desks** (`/admin/reassign`) — for a chosen day, force any
  employee's desk to a specific one (or send them to the waitlist),
  regardless of favorite/proximity rules. If the desk was already held by
  someone else that day, they're bumped to the waitlist and immediately
  re-evaluated by the normal algorithm (`promote_waitlist`) — they may land on
  another free desk rather than staying stuck. This does **not** manage the
  physical desk/zone inventory (still edited in `db.py::seed_desks`).
- **Password** (`/admin/password`) — change the admin password (current +
  new + confirm, minimum 8 characters).

## The assignment algorithm (`app/assignment.py`)

> For a deeper, example-driven walkthrough (including worked examples of the
> seniority tie-break), see [`ALGORITHM.md`](ALGORITHM.md).

### Physical model

Each `Desk` has an `x, y` position (map-diagram pixels, not real meters)
copied from the digitized floor plan (see `db.py::seed_desks`). There are no
fixed "zones" for teams — closeness between colleagues is computed from the
**real euclidean distance between desks**, not a zone system. Only two kinds
of desk have a fixed owner:

- `DESP-01`: the boss's office. Can only be assigned to him (`Employee.is_boss`).
- `HD-01/02/03`: reserved for whichever team is flagged `is_helpdesk` in the
  `Team` table (managed from the admin panel, see above).

### Instant assignment with full recalculation

There is no daily batch and no "pending" state: marking a day "In office"
assigns you a desk **immediately**, and every change (someone else declaring,
switching to remote/vacation) re-runs `resolve_day()` over **everyone**
declared for that day, from scratch:

- Boss gets `DESP-01`.
- Favorite desk, if free and eligible. If **two or more** employees share the
  same favorite, whoever has more seniority (`Employee.hire_date`) wins it
  outright, a deterministic tie-break with no dice roll. The loser is
  reseated elsewhere (never waitlisted just for losing a favorite). See
  [Seniority as a priority weight](#seniority-as-a-priority-weight).
- Everyone else: nearest free eligible desk to an already-seated teammate,
  else nearest to their own (lost) favorite as an anchor, else the lowest
  free desk code, processed in arrival order (`created_at`).
- No eligible desk left: waitlisted, with an email.

Because the algorithm is deterministic, re-running it on every change is
safe, and you always see your desk the moment you declare, instead of
waiting for a morning batch.

### The day lock

Each day **locks at `BOOKING_LOCK_HOUR:00`** (08:00 by default, server-local,
on the day itself): from then on, employees can no longer change that day's
choice (in office / remote / vacation). Future days stay open, and admins
can still override assignments after the lock from `/admin/reassign`.

### Vacation mode

A third `Mode`, alongside `presencial`/`teletrabajo`: `vacation`. It's
**functionally identical to remote work** — no desk is ever assigned, and
switching to it immediately frees any desk you're holding that day (and
triggers a full recalc, same as switching to remote). The only reason it
exists as its own value instead of reusing `teletrabajo` is so the map view
(below) can show "on vacation" separately from "chose to work remotely".

### Map view rosters (`/map`)

Besides the floor plan itself, `/map` shows who's doing what that day for
everyone not sitting at a desk box: **Remote**, **On vacation**,
**Waitlisted, no desk**, and **Unknown** (hasn't declared anything for that
day at all). Every employee shows up in exactly one place on the page,
either as a desk box, or in exactly one of these four buckets
(`mapview.py::build_map`).

### Email notifications

Two automatic emails, sent via `app/email.py::send_email` (real SMTP if
configured, console fallback otherwise, see "Environment variables"):

- **You've been waitlisted** — your presencial declaration couldn't be
  seated (office full), or an admin bumped you with nowhere else to go.
- **A desk opened up for you** — you were waitlisted and a recalc just
  seated you (someone switched to remote/vacation, or an admin freed a desk).

Desk-to-desk moves (e.g. someone more senior claimed your favorite) are
deliberately silent: you see your current desk when you open the app. Your
own click never emails you either, since getting a desk right when you
declare is visible on screen. See [`ALGORITHM.md`](ALGORITHM.md) for exactly
which code paths trigger each one.

### Seniority as a priority weight

Employees have an optional `hire_date` (set from `/admin/employees/{id}/edit`
— not self-editable). It converts into plain years of seniority
(`(today - hire_date).days / 365.25`, or `0` if unset) and is used **only**
to break ties when two or more people's favorite desk collides in the same
batch resolution — more years wins, no randomness involved. It plays no role
anywhere else (team-clustering fallback, late arrivals, waitlist promotion
all remain purely arrival-order based).

This replaced an earlier *probabilistic* "seniority reclaim" mechanic (a
capped % chance to retroactively steal a favorite desk from whoever had
already booked it that day). That approach still depended on booking order
— there had to be a "first" to steal from — and added randomness on top.
Now that a whole day's declarations are resolved together, there's no
"first"; every contested favorite is compared once, and seniority decides
outright. See [`ALGORITHM.md`](ALGORITHM.md) for the reasoning and worked
examples.

### Concurrent bookings (race condition)

Two people can try to declare/resolve the same slot almost simultaneously. To
keep that from ending in two people assigned to the same desk, `Booking` has
two database-level `UNIQUE` constraints (`models.py`):

- `(day, desk_id)` — never two *assigned* bookings for the same desk on the
  same day (`NULL`s from remote/waitlisted bookings don't collide
  with each other).
- `(employee_id, day)` — never two bookings for the same employee on the same
  day.

`book_day` retries up to `MAX_ASSIGN_RETRIES` times if the
`commit` collides with either constraint: each retry re-reads the real
database state, so a losing party falls through to the next candidate. If
there's still no room after the retries, the booking goes to the waitlist
instead of failing with an error.

On top of the constraints, every same-day mutation is serialized by a per-day
re-entrant lock (`assignment.py::_day_lock`): `book_day` and `admin_reassign`
hold it across their commit *and* their `resolve_day()` / `promote_waitlist()`
call. Without it, two concurrent bookings for the same day would each run
`resolve_day()` on a stale snapshot and could double-book a desk — the
`(day, desk_id)` constraint would then reject the second commit with an
`IntegrityError` (HTTP 500) — or leave a booking stuck with no status.
Different days use different locks and never block each other.

### Cancellations and the waitlist

`cancel_booking` frees the desk (if any), records the new mode (remote or
vacation), and calls `promote_waitlist(day)`, which walks through the people
waitlisted for that day **in arrival order** (`created_at`) and retries
`assign_desk` for each of them — the freed desk isn't forced on them, their
favorite/proximity is re-evaluated from scratch, in case there's a better
option for them. Anyone promoted this way gets the "a desk opened up for
you" email (see [Email notifications](#email-notifications)).

### What the algorithm deliberately does NOT do (and why)

- **No fixed zones per team.** An earlier version tried "zone inferred from
  majority of favorites", but it didn't match the real office (it's an open
  area, not separate rooms) and was replaced with direct physical distance —
  simpler and truer to how the room actually is.
- **No limit on remote/in-office days.** Bookings can be made up to 1 week
  ahead, with no minimum or maximum in-office days (an explicit decision, not
  an oversight).
- **No general seniority priority queue.** Seniority only decides contested
  favorite desks in the batch resolution — it plays no role in team
  clustering, late arrivals, or waitlist promotion, all of which remain
  purely arrival-order based, same as before this feature existed.
- **No optimal/combinatorial matching solver.** Each employee has at most one
  favorite desk (not a ranked list), and there are only 15 desks total —
  resolving favorite conflicts one at a time by seniority, then falling back
  to simple proximity, was a deliberate scope decision over something like
  the Hungarian algorithm, which would be overkill here.

## What's missing

- Admin screen to edit the physical desk/zone inventory without touching
  `db.py::seed_desks` (positions, adding/removing desks).
- Decide where it's hosted (not decided yet — the stack was chosen to be
  portable).
