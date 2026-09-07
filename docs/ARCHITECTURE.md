# Architecture

This document explains how Hotdesk ITS is put together: the components, how a
request flows through them, what each module is responsible for, the process
and threading model, the security model, and the error-handling strategy. For
the desk-assignment logic itself, see [ALGORITHM.md](ALGORITHM.md); for the
database schema, [DATABASE.md](DATABASE.md); for the full route reference,
[API.md](API.md); for running and deploying it, [DEPLOYMENT.md](DEPLOYMENT.md)
and [HTTPS.md](HTTPS.md).

---

## 1. What the app is, in one paragraph

Hotdesk ITS is an internal hot-desking app for the ITS office at IRB
Barcelona. There are fewer physical desks than employees, so every employee
declares, day by day, whether they will be in the office, working remotely, or
on vacation. The app assigns desks **instantly** — the moment someone declares
"in office" they are seated, and **every subsequent change re-plans the whole
day from scratch** for everyone who declared that day. There is no daily batch
job, no scheduler, and no "pending" state. The app is a single Python process
serving HTML pages (rendered server-side with Jinja2) to a browser; there is
no separate frontend, no API client, and no JavaScript build step.

---

## 2. Technology stack

| Layer | Choice | Why |
| --- | --- | --- |
| Language | Python 3.11 (pinned in `.python-version`) | Single-maintainer project; one language for everything. |
| Web framework | FastAPI | Dependency injection for the DB session and auth; `Form` parsing; clean route structure. |
| ORM / models | SQLModel (SQLAlchemy under the hood) | Models double as Pydantic schemas; `Session`-based access. |
| Database | SQLite, single file | No database server to operate; one file is trivially backed up. Concurrency is low (a few dozen staff, one writer process). |
| Templates | Jinja2 | Server-side rendering; the only "frontend" that exists. |
| Interactivity | HTMX 1.9.12 + Pico.css 2 (both from CDN) | The two real dynamic behaviors — toggling a day's mode without a full reload, and clicking the SVG map to pick a favorite desk — are HTMX swaps plus a few lines of vanilla JS. No bundler, no `node_modules`, no build step. |
| Sessions / tokens | itsdangerous (`URLSafeTimedSerializer`) | Signed, time-bounded cookies and magic-link tokens with no server-side session store. |
| Server | uvicorn (ASGI) | Serves the FastAPI app; in Docker it runs with `--proxy-headers` so it honors `X-Forwarded-Proto` behind a TLS-terminating reverse proxy. |
| Dependency management | `uv` + `uv.lock` | Reproducible installs; the Docker image installs exactly the locked versions (`uv sync --frozen`). |

The deliberate absence of a frontend pipeline is a core design decision: one
person maintains this app, it must open well on a phone, and every dependency
that would need a build step is a future maintenance tax. The only third-party
browser assets are two CDN `<script>`/`<link>` tags in `base.html`; the rest of
the look comes from one local stylesheet (`app/static/style.css`) — brand
blue, paper background, cards, wordmark, footer — pinned to a fixed light
theme with `<html data-theme="light">` so Pico 2 never follows the OS dark
mode (light text on the light paper background would make titles invisible).

---

## 3. Component map

```
                        ┌────────────────────────────────────────────┐
                        │                Browser (phone)             │
                        │  Pico.css + HTMX, no local JS build        │
                        └───────────────────▲────────────────────────┘
                                            │ HTML + HTMX swaps (HTTP/HTTPS)
                        ┌───────────────────┴────────────────────────┐
                        │        Reverse proxy (optional, HTTPS)     │
                        │   Caddy / nginx — terminates TLS,          │
                        │   forwards X-Forwarded-Proto/For           │
                        └───────────────────▲────────────────────────┘
                                            │ HTTP :8000
┌───────────────────────────────────────────┴────────────────────────────────┐
│                         uvicorn  ──  app.main:app                          │
│                                                                            │
│  ┌──────────┐  ┌───────────┐  ┌────────────┐  ┌───────────┐  ┌─────────┐  │
│  │ main.py  │  │ admin.py  │  │ auth.py    │  │ admin_    │  │ assign- │  │
│  │ employee │  │ admin     │  │ employee   │  │ auth.py   │  │ ment.py │  │
│  │ routes   │  │ routes    │  │ magic link │  │ admin     │  │ the     │  │
│  │          │  │           │  │ + password │  │ username/ │  │ desk    │  │
│  │          │  │           │  │ + session  │  │ password  │  │ engine  │  │
│  └────┬─────┘  └─────┬─────┘  └─────┬──────┘  └─────┬─────┘  └────┬────┘  │
│       └──────────────┴──────┬───────┴───────────────┴─────────────┘       │
│                             │                                              │
│        ┌────────────────────┼─────────────────────┐                        │
│        ▼                    ▼                     ▼                        │
│  ┌───────────┐      ┌────────────┐        ┌───────────────┐                │
│  │ models.py │      │  db.py     │        │  email.py     │                │
│  │ SQLModel  │      │ engine,    │        │  SMTP send +  │                │
│  │ tables    │      │ seed,      │        │  console      │                │
│  │ + enums   │      │ migration  │        │  fallback     │                │
│  └───────────┘      └─────┬──────┘        └───────────────┘                │
│                           │                                                 │
│                           ▼                                                 │
│                  ┌─────────────────┐      ┌────────────────────────────┐   │
│                  │   SQLite file   │      │  security.py  (PBKDF2 +    │   │
│                  │  hotdesk.db     │      │   SECRET_KEY, shared by    │   │
│                  │  (HOTDESK_DB)   │      │   auth.py / admin_auth.py) │   │
│                  └─────────────────┘      └────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────────┘
```

Supporting modules that don't own routes:

- `mapview.py` — turns a day's booking state into the data the SVG map
  template draws (desk boxes, zone labels, and the four-bucket roster of
  everyone not sitting at a desk).
- `dates.py` — "upcoming weekdays" and day-picker resolution, shared by the
  employee and admin day views.
- `forms.py` — form/query parsing helpers (dates, optional ints) that turn
  garbage input into HTTP 400 instead of an unhandled 500.
- `templating.py` — the single shared Jinja2Templates instance.
- `templates/` — all the HTML. The office SVG map (desk and pillar layout)
  lives in `_office_landmarks.html`, included by both `map.html` (view
  occupancy) and `settings.html` (pick a favorite by clicking).

---

## 4. Module responsibilities

```
app/
  models.py        SQLModel tables (Employee, Desk, Zone, Team, Booking,
                   DeskRequest, MagicLink, AdminUser) and the enums
                   (Reserved, Mode, WorkPattern, BookingStatus,
                   DeskRequestStatus). Pure data definitions — no logic.
  db.py            SQLite engine (path from HOTDESK_DB), get_session()
                   dependency, init_db() (create_all + column migration +
                   seed), and the seed data: 15 desks digitized from the real
                   floor plan, 4 zones, 5 default teams, default admin.
  security.py      Password hashing (PBKDF2-HMAC-SHA256, 260 000 iterations)
                   and the SECRET_KEY used to sign both session cookie
                   families. Deliberately has no DB/model imports so db.py
                   (seeding the default admin) and admin_auth.py (verifying
                   logins) can both use it without an import cycle.
  email.py         SMTP configuration from env vars + send_email(). Real SMTP
                   when SMTP_HOST is set; console fallback otherwise.
  auth.py          Employee authentication: magic-link creation/verification
                   (registration and reset), password set/verify, the signed
                   "session" cookie, and the current_employee /
                   optional_employee FastAPI dependencies.
  admin_auth.py    Admin authentication: username/password login, its own
                   signed "admin_session" cookie (different itsdangerous
                   salt, different max age), and the current_admin /
                   optional_admin dependencies.
  admin.py         Admin panel routes (dashboard, employees, teams, desk
                   reassignment, password change) on the /admin prefix.
  assignment.py    The desk assignment engine: resolve_day, assign_desk,
                   promote_waitlist, admin_reassign, book_day,
                   ensure_present_bookings (in-person auto-booking), the day
                   lock, the per-day re-entrant locks, and the two waitlist
                   notifications. The heart of the app — see ALGORITHM.md.
  desk_requests.py "Request this desk": an employee asks the occupant of a
                   desk to cede it (DeskRequest rows, the /map/request and
                   /requests/{id} routes, the four request emails). Accepting
                   reuses assignment.admin_reassign.
  mapview.py       Builds the occupancy data for the /map view (including the
                   can_request flag that drives the "request this desk"
                   button).
  dates.py         Shared "upcoming weekdays" + day-picker helpers.
  forms.py         Form/query parsing helpers (dates, optional ints).
  templating.py    The single shared Jinja2Templates instance.
  main.py          The FastAPI app object and the employee-facing routes
                   (login, set-password, forgot, auth/verify, settings,
                   calendar, map, booking, the year-long vacation calendar).
                   Includes the admin router.
  templates/       Jinja2 + HTMX. _office_landmarks.html holds the SVG floor
                   plan; admin_*.html are the admin panel pages.
```

Dependency direction is strictly downward: routes (`main.py`, `admin.py`) →
domain logic (`assignment.py`, `auth.py`, `admin_auth.py`, `mapview.py`) →
persistence and config (`db.py`, `models.py`, `security.py`, `email.py`).
`security.py` sits at the bottom on purpose — it imports nothing from the
app so it can be shared without cycles.

---

## 5. Request flow

### 5.1 An employee books a day

```
Browser ──POST /calendar/book {day, mode}──▶ main.py::calendar_book
  1. current_employee dependency: read the signed "session" cookie, verify
     signature + max age (itsdangerous), load the Employee row. 401 if bad.
  2. Validate mode (must be a Mode enum) and day (must be within the
     upcoming-weekdays window). 400 on either failure.
  3. assignment.py::book_day(session, employee, day, mode)
       a. is_day_locked(day)? → DayLockedError → 403.
       b. Take the per-day re-entrant lock for `day` (serializes all
          same-day mutations).
       c. Write/refresh the Booking row, commit (retry up to 3× on
          IntegrityError from the two UNIQUE constraints).
       d. If the change affects seating (presencial declared, or a desk
          freed), resolve_day(day): recompute every presencial assignment
          for that day from scratch, commit, then send waitlist
          transition emails (best-effort).
       e. Release the lock.
  4. Render _day_row.html (the one day's row) and return it. HTMX swaps it
     into the calendar in place — no full page reload.
```

### 5.4 An employee requests a desk (cede flow)

```
Browser ──POST /map/request {day, desk_id}──▶ desk_requests.py::map_request
  1. current_employee dependency. 401 if bad.
  2. Validate: day in window (400), desk exists (404), desk occupied that
     day (400), not the viewer's own desk (400), viewer eligible for the
     desk (400).
  3. Create DeskRequest(pending) — requester, occupant (the current holder),
     desk, day.
  4. Email the occupant a link to /requests/{id} (best-effort).
  5. 303 back to /map?day=…

Occupant clicks the link (normal login required — the link grants nothing):
Browser ──GET /requests/{id}──▶ desk_requests.py::request_view
  1. 404 if missing; 403 if the viewer is neither requester nor occupant.
  2. If the request is pending but stale (desk changed hands, day passed),
     expire it on the spot and notify the requester.
  3. Render the decision page (Accept/Decline only for a valid pending
     request viewed by the occupant).

Occupant decides:
Browser ──POST /requests/{id}/decide {action}──▶ desk_requests.py::request_decide
  1. 403 unless the viewer is the occupant. Stale → expire + 400.
  2. accept: assignment.admin_reassign(requester, day, desk) — the requester
     is seated at the desk, the occupant is bumped and re-seated surgically
     (promote_waitlist). Bypasses the day lock on purpose (mutual
     agreement). Request → accepted; requester emailed.
   3. decline: request → declined; requester emailed. Nobody moves.
 ```

### 5.5 An employee marks vacation days / is booked in automatically

```
Vacation (year-long calendar):
Browser ──GET /vacation?month=YYYY-MM──▶ main.py::vacation_view
   1. current_employee. Month validated: must be the current year, else 400.
   2. Render the month grid (_vacation_grid.html): past days, weekends and
      today-after-lock are muted; every other weekday is a toggle button.

Browser ──POST /vacation/toggle {day}──▶ main.py::vacation_toggle
   1. Validate: weekday of the current year, day >= today (400s); today at
      or past the lock hour → 403.
   2. Toggle: no booking → create Booking(mode=vacation); vacation booking
      → delete it (a presencial-pattern employee with a window-day gets the
      automatic in-office booking back, which re-plans the day);
      presencial booking → becomes vacation, freed desk re-plans the day.
      Retries on IntegrityError like book_day.
   3. Return the month grid (HTMX swaps it in). Marks outside the 5-day
      window are stored as-is; they apply when the day reaches the window.

In-person auto-booking (no browser involved):
   Every render of a day (calendar, map, admin dashboard, admin reassign)
   and every book_day first runs
   assignment.py::ensure_present_bookings(session, day): create the missing
   "in office" bookings for work_pattern=presencial employees with complete
   profiles (skipping anyone already booked — vacation wins) and re-plan the
   day once if anything was created. Lock-exempt: a standing declaration,
   not a last-minute change.
 ```

### 5.2 A first-time employee logs in

```
Browser ──POST /login {email, password=""}──▶ main.py::login_submit
  1. No Employee row (or no password yet) → auth.py::request_login
       a. create_magic_link: token = secrets.token_urlsafe(32), stored in
          MagicLink with a 15-minute expiry and purpose="login".
       b. send_email with the /auth/verify?token=… URL (real SMTP or
          console fallback).
  2. Render check_email.html.

Employee clicks the email link:
Browser ──GET /auth/verify?token=…──▶ main.py::auth_verify
  1. verify_token: look up the MagicLink; 400 if missing/used/expired.
     Auto-create the Employee row if it doesn't exist (this is how
     registration works).
  2. The employee has no password yet → redirect to /set-password?token=…
  3. POST /set-password {token, new_password, confirm_password}:
     validate (≥8 chars, match), set_password() consumes the token and
     stores the PBKDF2 hash+salt, then set the session cookie and 303 to
     /settings (profile is incomplete) or /map.
```

### 5.3 An admin reassigns a desk

```
Browser ──POST /admin/reassign {day, employee_id, desk_id}──▶ admin.py
  1. current_admin dependency: verify the "admin_session" cookie. 401 if bad.
  2. assignment.py::admin_reassign(session, employee, day, desk_id)
       a. Take the per-day lock.
       b. If another employee holds desk_id that day, bump them to the
          waitlist.
       c. Force the target employee's booking to desk_id (or waitlist).
       d. promote_waitlist(day): retry only the waitlisted bookings, in
          arrival order, against whatever is now free. Everyone else keeps
          their desk — admin overrides are surgical, they never reshuffle
          the whole office.
       e. Release the lock; email anyone who entered the waitlist.
```

---

## 6. Process and threading model

- **One process, one writer.** The app runs as a single uvicorn process.
  SQLite's locking model is fine for this: a few dozen staff, and every
  same-day mutation is serialized in-process anyway (below). There is no
  connection pool tuning, no WAL mode, no replication — the database is one
  file (see [DATABASE.md](DATABASE.md) for backup/restore).
- **uvicorn's threadpool.** FastAPI runs its synchronous route functions in
  a threadpool, so two requests can execute concurrently in different
  threads. That is exactly why the assignment engine carries its own
  locks — see next.
- **Per-day re-entrant locks** (`assignment.py::_day_lock`). Every
  same-day mutation — `book_day`, `resolve_day`, `promote_waitlist`,
  `admin_reassign` — runs under an `RLock` keyed by the date. `RLock` (not
  `Lock`) because a high-level function and the resolver it calls both
  acquire the same day's lock. Different days get different locks and never
  block each other. Without these, two concurrent same-day bookings would
  each run `resolve_day` on a stale snapshot and could double-book a desk
  (the `(day, desk_id)` UNIQUE constraint would then reject the second
  commit with an `IntegrityError` → HTTP 500) or leave a booking stuck with
  no status.
- **Database constraints as the last line of defense.** Even with the locks,
  `Booking` carries two `UNIQUE` constraints — `(day, desk_id)` and
  `(employee_id, day)` — so a race can at worst become an `IntegrityError`
  on commit, which `book_day` retries a bounded number of times against
  fresh state. The locks make the race rare; the constraints make it
  harmless.
- **No background workers.** There is no scheduler, no Celery, no cron.
  "Re-plan the day" happens synchronously inside the request that changed
  it. Emails are sent inline (best-effort) after the commit.

---

## 7. Security model

There are **two independent authentication systems** that coexist in the
same browser via two different cookies:

| | Employee | Admin |
| --- | --- | --- |
| Credential | Email + password (after a one-time magic-link setup) | Username + password |
| Cookie name | `session` | `admin_session` |
| itsdangerous salt | (default) | `"admin-session"` |
| Cookie max age | 30 days | 12 hours |
| Cookie flags | `HttpOnly`, `SameSite=Lax`, `Secure` when the request arrived over HTTPS | same |
| Password storage | PBKDF2-HMAC-SHA256, 260 000 iterations, 16-byte random salt | same |
| Dependencies | `current_employee` / `optional_employee` | `current_admin` / `optional_admin` |

### 7.1 Passwords

- Hashing is `hashlib.pbkdf2_hmac("sha256", password, salt, 260_000)` with a
  `secrets.token_hex(16)` salt; verification recomputes and compares with
  `secrets.compare_digest` (constant-time). The iteration count is the OWASP
  recommendation for PBKDF2-HMAC-SHA256.
- Minimum length is 8 characters, enforced at both `/set-password` and
  `/admin/password`.
- The default admin account (`admin` / `admin123`) is created only on first
  seed and printed once to the server console; the documentation treats
  changing it as a mandatory first step.

### 7.2 Magic links

- A magic link is a `MagicLink` row: a `secrets.token_urlsafe(32)` token,
  the target email, a 15-minute expiry, a `used_at` (single-use), and a
  `purpose` (`"login"` for first registration, `"reset"` for forgot
  password).
- `verify_token` rejects a link that is missing, already used, or expired
  (HTTP 400). Verification is **idempotent when `consume=False`** — the
  `GET /set-password` handler validates the token without consuming it, so
  the form can be re-rendered after a failed password confirmation.
- The token is carried in the URL query string. To limit leakage via
  `Referer` headers, `base.html` sets `<meta name="referrer"
  content="no-referrer">`.
- Links are emailed, never shown on screen, except in local development
  where `SMTP_HOST` is unset and `send_email` prints them to the server
  console instead.

### 7.3 Anti-enumeration on employee login

`POST /login` with a wrong password returns **HTTP 200 with a generic
"Invalid email or password" message**, not 401. This is deliberate: a
distinct status code for "password wrong" vs. "no such email / no password
yet" would let an attacker map which emails have an account with a password
set. It is an accepted trade-off for an internal app with known staff — the
admin login (`/admin/login`) does return 401, since the admin userbase is
tiny and the enumeration risk is not the same.

### 7.4 Session cookies

Both cookie values are itsdangerous-signed payloads (`{"employee_id": …}` /
`{"admin_id": …}`), so there is no server-side session store to invalidate —
a cookie is valid until it expires or the `SECRET_KEY` changes. Rotating
`SECRET_KEY` logs everyone out. The `Secure` flag is set automatically when
the request's scheme is `https`, which is why the app must run behind a
proxy that forwards `X-Forwarded-Proto` (see [HTTPS.md](HTTPS.md)).

### 7.5 What is out of scope (by design)

- No rate limiting on login endpoints (internal app, known staff).
- No CSRF tokens — protection comes from `SameSite=Lax` cookies plus the
  fact that all state-changing routes are `POST` form submissions.
- No per-employee authorization beyond "you can edit your own profile and
  book your own days"; everything under `/admin` requires the admin cookie.

---

## 8. Error-handling strategy

- **Bad user input → 400, never 500.** `forms.py` centralizes parsing so a
  malformed date or int becomes an `HTTPException(400)` instead of an
  unhandled `ValueError`.
- **Auth failures → 401** from the dependencies (`current_employee`,
  `current_admin`), except the employee login form itself, which returns 200
  with an inline error (see §7.3).
- **Domain rule violations → 403** (booking a locked day) or **400**
  (invalid team, invalid mode, duplicate email/team, deleting the helpdesk
  team, wrong current password).
- **Concurrency conflicts → retried, then degraded.** `book_day` retries up
  to `MAX_ASSIGN_RETRIES` (3) times on `IntegrityError`; if it still cannot
  record the booking it returns 503 "please retry" rather than corrupting
  state.
- **Email is best-effort where it must not block.** `send_email` takes
  `raise_on_error`: magic links pass the default `True` (a failed send
  becomes a 500, because the user is staring at a "check your email" page
  that would otherwise lie), while waitlist notifications pass `False` (a
  failed notification is logged and the booking that triggered it still
  succeeds).
- **Database integrity is enforced by constraints, not by application
  checks alone** — the two `Booking` UNIQUE constraints are the actual
  double-booking guard (see [ALGORITHM.md](ALGORITHM.md) §Race conditions).

---

## 9. Configuration surface

All configuration is via environment variables — there is no config file,
no `.env` auto-loading (the app reads the process environment only; see
[DEPLOYMENT.md](DEPLOYMENT.md) for how each deployment target supplies it).

| Variable | Default | Effect |
| --- | --- | --- |
| `SECRET_KEY` | `dev-secret-change-me` | Signs both session cookie families and the magic-link serializer. **Must** be a real secret in production; rotating it invalidates all sessions. |
| `HOTDESK_DB` | `<repo>/hotdesk.db` | Path of the SQLite file. In Docker it is `/data/hotdesk.db` (a bind-mounted host directory). |
| `SMTP_HOST` | unset | If unset, `send_email` prints to the console instead of sending (dev mode). |
| `SMTP_PORT` | `587` | SMTP port. A non-integer value falls back to 587. |
| `SMTP_USER` | unset | SMTP auth username. Login is attempted only when both user and password are set. |
| `SMTP_PASSWORD` | unset | SMTP auth password. |
| `SMTP_FROM` | `SMTP_USER` or `hotdesk@localhost` | `From:` address on outgoing mail. |
| `SMTP_USE_TLS` | `true` | Whether to `STARTTLS` before sending. `0`/`false`/`no` disables it. |
| `BOOKING_LOCK_HOUR` | `8` | Server-local hour (0–23) at which a day stops accepting employee changes. |

---

## 10. Known limitations and deliberate non-goals

- **Single process only.** The in-process per-day locks do not coordinate
  across multiple uvicorn workers or multiple containers. Run one process;
  scale by adding machines only if you also move to a real database and
  cross-process locking.
- **SQLite on a network filesystem** (NFS/Lustre) can misbehave under
  concurrent writes; local disk is the safe choice (see
  [DEPLOYMENT.md](DEPLOYMENT.md)).
- **No API.** Everything is HTML form posts and HTMX swaps. There is no
  JSON endpoint; the "API" is the route table in [API.md](API.md).
- **No multi-tenant / multi-office support.** The desk inventory is seeded
  from one specific floor plan (ITS office 01A06); changing it means editing
  `db.py::seed_desks` (an admin screen for the desk inventory is a known
  missing feature, see the README).
