# API Reference

Complete reference for every route in the app. There is no JSON API — every
route renders HTML (or an HTMX fragment) or issues a redirect. "Auth" refers
to the FastAPI dependency that guards the route:

- **Employee** — requires a valid `session` cookie (`current_employee`);
  unauthenticated requests get **401**.
- **Optional employee** — `optional_employee`; the route behaves differently
  for anonymous visitors but does not reject them.
- **Admin** — requires a valid `admin_session` cookie (`current_admin`);
  unauthenticated requests get **401**.
- **Public** — no authentication.

Status-code conventions used throughout:

| Code | Meaning in this app |
| --- | --- |
| 200 | HTML page or HTMX fragment rendered successfully. Also used for a *failed* employee login (deliberate anti-enumeration choice — see below). |
| 303 | "See Other" redirect after a successful `POST` (PRG pattern). All successful form submissions redirect with 303. |
| 400 | Bad input (invalid date, invalid mode, invalid team, validation failure, invalid/expired magic link). |
| 401 | Missing/invalid session cookie (dependency rejection) or failed admin login. |
| 403 | Domain rule violation — booking a day that is already locked. |
| 404 | Resource not found (employee id, etc.). |
| 500 | Email could not be sent on a magic-link request (`raise_on_error=True`). |
| 503 | `book_day` could not record the booking after 3 retries (concurrent writes). |

---

## Employee routes

### `GET /`

**Auth:** optional employee.

Redirects based on state:

| State | Redirects to |
| --- | --- |
| Not logged in | `/login` |
| Logged in, profile incomplete (missing name, surname, or team) | `/settings` |
| Logged in, profile complete | `/map` |

### `GET /login`

**Auth:** public.

Renders the login form (`login.html`).

### `POST /login`

**Auth:** public.

**Form fields:**

| Field | Type | Notes |
| --- | --- | --- |
| `email` | string, required | Normalized: trimmed and lowercased. |
| `password` | string, optional (defaults to `""`) | Empty = "I don't have a password yet" → magic-link flow. |

**Behavior:**

1. If an `Employee` with that email exists **and** has a password set:
   - Password correct → set the `session` cookie and **303** to `/map`
     (or `/settings` if the profile is incomplete).
   - Password wrong → **200** re-rendering the login form with
     "Invalid email or password". **Not 401**: returning a distinct status
     for "password wrong" would let an attacker enumerate which emails have
     a password set. Deliberate trade-off for an internal app.
2. If no employee exists, or the employee has no password yet (legacy
   account) → `request_login` creates a magic link (`purpose="login"`,
   15-minute TTL) and emails it (or prints it to the console when
   `SMTP_HOST` is unset), then **200** renders `check_email.html`.
   - If the email cannot be sent, **500** (the "check your email" page
     would otherwise lie).

### `POST /forgot`

**Auth:** public.

**Form fields:** `email` (required).

Creates a magic link with `purpose="reset"` and emails it; renders
`check_email.html`. **200** on success, **500** if the email cannot be sent.
Note: it does not reveal whether the email exists — the same page is shown
either way (the reset link, if the account exists, simply overwrites the
password after the user sets a new one).

### `GET /auth/verify?token=…`

**Auth:** public (the token is the credential).

**Query params:** `token` (the magic-link token from the email).

**Behavior:**

1. `verify_token(consume=False)`: **400** if the token is missing, already
   used, or expired.
2. The `Employee` row is created on the fly if it doesn't exist (this is
   how self-registration works).
3. If the link's `purpose` is `"reset"`, or the employee has no password
   yet (first registration) → redirect to `/set-password?token=…` — the
   user must choose a password before entering the app.
4. Otherwise (a `"login"` link for an account that already has a password)
   → the link is consumed (`used_at` set) and the `session` cookie is set;
   **303** to `/map` or `/settings`.

### `GET /set-password?token=…`

**Auth:** public (the token is the credential).

**Query params:** `token`.

Validates the token **without consuming it** (`consume=False`): **400** with
an "Invalid or expired link" message if bad, otherwise renders the
set-password form. Validating without consuming lets the form be re-rendered
after a failed submission (wrong length / mismatch) without burning the
link.

### `POST /set-password`

**Auth:** public (the token is the credential).

**Form fields:**

| Field | Type | Validation |
| --- | --- | --- |
| `token` | string, required | Must be a valid, unused, unexpired magic link. |
| `new_password` | string, required | ≥ 8 characters. |
| `confirm_password` | string, required | Must equal `new_password`. |

**Behavior:** validation failures return **400** re-rendering the form with
the specific error (the token is **not** consumed, so the user can retry).
On success the token is consumed, the PBKDF2 hash+salt is stored on the
employee, the `session` cookie is set, and the response is **303** to
`/map` or `/settings`.

### `GET /logout`

**Auth:** public.

Deletes the `session` cookie and **303** to `/login`.

### `GET /profile`

**Auth:** public.

Compatibility redirect: **303** to `/settings` (the profile form moved).

### `GET /settings`

**Auth:** employee.

Renders the profile form (`settings.html`): name, surname, team (select),
favorite desk (picked by clicking the SVG map — HTMX + a few lines of
vanilla JS), and an optional hire-date **proposal**. The desk picker excludes
the boss's desk (`DESP-01`); helpdesk desks are selectable by anyone in the
form but an ineligible choice is silently ignored on submit (below).

### `POST /settings` and `POST /profile`

**Auth:** employee. Both paths hit the same handler.

**Form fields:**

| Field | Type | Notes |
| --- | --- | --- |
| `name` | string, required | Trimmed. |
| `surname` | string, required | Trimmed. |
| `team_id` | string, required | Must reference an existing team, else **400**. |
| `favorite_desk_id` | string, optional | `""` clears the favorite. A helpdesk desk chosen by a non-helpdesk employee is silently cleared (ineligible choice ignored rather than errored). |
| `hire_date` | string (ISO date), optional | **Proposed** hire date. Stored in `hire_date_proposed`, not `hire_date` — it only counts as seniority after an admin approves it (see [ALGORITHM.md](ALGORITHM.md) §Seniority). Setting it to the already-approved value clears the proposal. |

**Behavior:** saves the profile, **303** to `/map`.

### `GET /calendar`

**Auth:** employee.

Renders the week planner (`calendar.html`): the upcoming weekdays (today +
6 days, weekdays only — up to 7 Mon–Fri days), each with the employee's
current booking, the assigned desk code (if any), and a lock indicator.
Days at/past `BOOKING_LOCK_HOUR` are rendered as locked (the day's row is
disabled).

### `GET /map?day=…`

**Auth:** employee.

**Query params:** `day` (optional ISO date). Must be one of the upcoming
weekdays; anything else (missing, malformed, out of window) falls back to
the first selectable day — never an error.

Renders the office map (`map.html`): the SVG floor plan with every desk
colored by status (`free`, `mine`, `occupied`, `reserved-empty`), zone
labels, and the four-bucket roster for everyone not sitting at a desk
(`Remote`, `On vacation`, `Waitlisted, no desk`, `Unknown`). See
[ALGORITHM.md](ALGORITHM.md) §Map view rosters.

### `POST /calendar/book`

**Auth:** employee.

**Form fields:**

| Field | Type | Notes |
| --- | --- | --- |
| `day` | string (ISO date), required | Must be within the upcoming-weekdays window, else **400**. |
| `mode` | string, required | Must be `presencial`, `teletrabajo`, or `vacation`, else **400**. |

**Behavior:** calls `book_day` (see [ALGORITHM.md](ALGORITHM.md)):

- Day already locked → **403** with the lock message.
- Success → **200** returning `_day_row.html` (just that day's row), which
  HTMX swaps into the calendar in place.
- Could not record after 3 concurrency retries → **503** "please retry".

For `presencial` the response may show an assigned desk code or
"waitlisted"; for `teletrabajo`/`vacation` it shows the mode with no desk.

---

## Desk requests ("request this desk")

An employee who wants a specific occupied desk can ask its current holder to
cede it. The occupant gets an email with a link to a decision page
(`/requests/{id}`); the link itself grants nothing — the page requires the
occupant's normal login. Accepting seats the requester at the desk and
re-seats the occupant surgically (nobody else moves); declining just
notifies the requester. A request that goes stale — the desk changed hands,
or the day passed — is marked `expired` lazily (when viewed or decided) and
the requester is notified. Accepting deliberately **bypasses the day lock**:
a cede is a mutual agreement between two people who are both affected (same
semantics as an admin override).

The map's "Who's where" list shows a **Request this desk** button on every
occupied desk the viewer is eligible for (never on their own desk, never on
reserved desks they can't use).

### `POST /map/request`

**Auth:** employee.

**Form fields:**

| Field | Type | Notes |
| --- | --- | --- |
| `day` | string (ISO date), required | Must be within the upcoming-weekdays window, else **400**. |
| `desk_id` | int, required | **404** if unknown. |

**Behavior:** **400** if the desk is not occupied that day, if it is the
viewer's own desk, or if the viewer is not eligible for it (boss desk,
helpdesk desks). On success a `DeskRequest` row is created (`pending`) and
the occupant is emailed with a link to `/requests/{id}` (best-effort).
**303** back to `/map?day=…`.

### `GET /requests/{request_id}`

**Auth:** employee. **404** if the request doesn't exist; **403** if the
viewer is neither the requester nor the occupant.

Renders the request details (who, which desk, which day, status). If the
viewer is the occupant and the request is still `pending` **and still valid**
(the desk is still held by them, the day is still in the window), the page
shows **Accept** / **Decline** buttons. If a pending request is found stale,
it is expired on the spot and the requester is notified.

### `POST /requests/{request_id}/decide`

**Auth:** employee — and specifically the **occupant**: **403** for anyone
else (including the requester).

**Form fields:** `action` — `accept` or anything else (treated as decline).

**Behavior:** **404** if missing. If the request is no longer valid it is
expired (if still pending) and **400** is returned. Otherwise:

- `accept` → `admin_reassign(requester, day, desk)`: the requester is
  declared presencial (if not already) and seated at the desk; the occupant
  is bumped to the waitlist and re-seated by `promote_waitlist` (surgical —
  nobody else moves). The request becomes `accepted` and the requester is
  emailed.
- `decline` → the request becomes `declined` and the requester is emailed.
  Nobody moves.

**303** back to `/map?day=…` in both cases.

---

## Admin routes

All under the `/admin` prefix, all requiring the `admin_session` cookie
except the login/logout pair.

### `GET /admin/login`

**Auth:** public. Renders the admin login form.

### `POST /admin/login`

**Auth:** public.

**Form fields:** `username` (trimmed), `password`.

- Valid → set the `admin_session` cookie (12-hour max age) and **303** to
  `/admin`.
- Invalid → **401** re-rendering the form with "Invalid username or
  password". (Unlike employee login, this *does* use 401 — the admin
  userbase is tiny.)

### `GET /admin/logout`

**Auth:** public. Deletes the `admin_session` cookie, **303** to
`/admin/login`.

### `GET /admin`

**Auth:** admin.

Dashboard: employee count, team count, and today's booking stats (assigned
/ waitlisted / remote / vacation).

### `GET /admin/employees`

**Auth:** admin.

Lists all employees with team, boss flag, hire date, pending hire-date
proposal (with **Approve**/**Reject** buttons), and an edit/delete link per
row. Also hosts the "add employee" form.

### `POST /admin/employees`

**Auth:** admin.

**Form fields:**

| Field | Type | Notes |
| --- | --- | --- |
| `email` | string, required | Trimmed, lowercased. Must be unique, else **400**. |
| `name` | string, optional | |
| `surname` | string, optional | |
| `team_id` | string, optional | |
| `is_boss` | checkbox | Defaults to `False`. |
| `hire_date` | string (ISO date), optional | Set directly (admin-set seniority needs no approval). |

**Behavior:** creates the employee (**303** back to the list). A duplicate
email — caught either by the pre-check or by the DB `UNIQUE` constraint as
a race — returns **400**.

### `GET /admin/employees/{employee_id}/edit`

**Auth:** admin.

**404** if the employee doesn't exist. Renders the edit form; if the
employee has a pending `hire_date_proposed`, the form shows it and warns
that saving the form clears it.

### `POST /admin/employees/{employee_id}/edit`

**Auth:** admin.

**Form fields:** same as add, minus `email` (immutable here): `name`,
`surname`, `team_id`, `is_boss`, `hire_date`.

**Behavior:** **404** if missing. Saves the fields; setting any field
supersedes a pending proposal (`hire_date_proposed` is cleared). **303**
back to the list.

### `POST /admin/employees/{employee_id}/hire_date_proposed`

**Auth:** admin.

**Form fields:** `action` — `approve` or anything else (treated as reject).

**Behavior:** **404** if missing. `approve` copies
`hire_date_proposed` → `hire_date` (only if a proposal exists); either way
the proposal is cleared. **303** back to the list.

### `POST /admin/employees/{employee_id}/delete`

**Auth:** admin.

**Behavior:** deletes the employee, all their bookings, and all their magic
links, then runs `promote_waitlist` for every day they had a booking on — a
desk freed by the deletion must not sit empty (see
[ALGORITHM.md](ALGORITHM.md) §What happens when a desk frees up). **303**
back to the list. Deleting a non-existent id is a no-op that still
redirects.

### `GET /admin/teams`

**Auth:** admin.

Lists teams with member counts (one `GROUP BY` query, not one `COUNT` per
team) and the "add team" form. The team flagged `is_helpdesk` is marked —
its members are the only ones eligible for the `HD-01/02/03` reserved desks.

### `POST /admin/teams`

**Auth:** admin.

**Form fields:** `name` (required, trimmed), `is_helpdesk` (checkbox).

**400** on: empty name, duplicate name, or a second helpdesk team (at most
one team may be flagged). **303** on success.

### `POST /admin/teams/{team_id}/delete`

**Auth:** admin.

**400** if it's the helpdesk team (delete the flag or reassign it first).
Otherwise the team's employees are **unassigned** (`team_id = NULL`) rather
than blocking the deletion — they keep their account and pick a new team
from their profile. **303** on success.

### `GET /admin/reassign?day=…`

**Auth:** admin.

**Query params:** `day` (optional ISO date; same fallback rules as
`/map`).

Renders the reassignment table for the chosen day: every employee, their
current status label (Assigned / Waitlisted / Remote / Vacation / No
booking), their current desk, and a select of available desks (their current
desk plus every unassigned desk).

### `POST /admin/reassign`

**Auth:** admin.

**Form fields:**

| Field | Type | Notes |
| --- | --- | --- |
| `day` | string (ISO date), required | |
| `employee_id` | int, required | **404** if unknown. |
| `desk_id` | string, optional | `""` = send to the waitlist. |

**Behavior:** calls `admin_reassign` (see [ALGORITHM.md](ALGORITHM.md)
§Admin overrides): forces the desk (or waitlist) regardless of rules **and
regardless of the day lock**; the previous holder of that desk, if any, is
bumped to the waitlist and immediately re-evaluated by `promote_waitlist`.
**303** back to the reassign view for that day.

### `GET /admin/password`

**Auth:** admin. Renders the password-change form.

### `POST /admin/password`

**Auth:** admin.

**Form fields:** `current_password`, `new_password`, `confirm_password`.

**400** (form re-rendered with the specific error) on: wrong current
password, new password < 8 characters, or mismatch. On success the new
PBKDF2 hash+salt is stored and the form re-renders with "Password updated".
The current `admin_session` cookie stays valid (it references the admin id,
not the password).

---

## Static files

`/static` is mounted **only if** `app/static/` exists (it does — the single
app stylesheet `style.css` lives there and is linked from `base.html`).
Everything else is the Pico.css CDN, the HTMX CDN script, and inline SVG.
