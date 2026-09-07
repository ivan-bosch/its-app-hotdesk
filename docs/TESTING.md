# Testing

How Hotdesk ITS is tested: the suites, what each one covers, how to run
them, the design of the harness, and how to add a test.

---

## 1. The suites

Five end-to-end suites live in `tests/`. Each one drives the **real**
FastAPI app against a **real** SQLite database — no mocks, no stubs, no
pytest. They are plain Python scripts with their own tiny runner:

```bash
uv run --with httpx python tests/test_password_login.py
uv run --with httpx python tests/test_seniority_flow.py
uv run --with httpx python tests/test_home_settings.py
uv run --with httpx python tests/test_desk_requests.py
uv run --with httpx python tests/test_work_patterns.py
```

`--with httpx` is needed because `fastapi.testclient.TestClient` requires
httpx, which is not a project dependency (it's only a test-time need). Each
suite prints one `[PASS]`/`[FAIL]` line per check, a summary, and exits
non-zero if anything failed — so they can be chained in CI or a shell loop:

```bash
for t in tests/test_*.py; do uv run --with httpx python "$t" || exit 1; done
```

### 1.1 `tests/test_password_login.py` — 13 checks

The employee authentication flow (magic link → forced password →
email+password login → forgot password):

| Check | What it proves |
| --- | --- |
| registro redirige a /set-password | First registration must end at `/set-password`, not in the app. |
| set-password crea credenciales + sesion | A successful set-password stores the PBKDF2 hash+salt **and** starts a session (303 + `session` cookie). |
| login con password correcto | Email+password login works after setup (303 + cookie). |
| login con password incorrecto | Wrong password → **200** (not 401) with the generic "Invalid email or password" and **no** cookie — the anti-enumeration behavior. |
| forgot password resetea | A reset link sets a new password; the old one stops working (200, no session) and the new one logs in (303). |
| cuenta legacy sin password → link → /set-password | An employee created by an admin without a password still goes through the magic-link → set-password flow on first login. |
| set-password valida longitud y confirmacion | Short password and mismatch both → 400, and **no** hash is stored. |
| token de set-password single-use | First use → 303; second use of the same token → 400. |
| link login con password existente → app | A `purpose="login"` link for an account that *already* has a password enters the app directly (defensive path). |
| password vacio no es bypass ni envia link | Submitting `/login` with an empty password for an account that has one does **not** create a session, and does **not** issue a new magic link (no link-count growth). |
| validacion fallida no consume el token | A failed set-password (400) leaves the token usable — the retry succeeds (303). |
| token expirado rechazado en verify | A link whose `expires_at` is in the past → 400 at `/auth/verify`. |
| migracion esquema antiguo | The DB was pre-created with the *old* schema (no password columns, no `magiclink.purpose`); after startup, the new columns exist. |

### 1.2 `tests/test_seniority_flow.py` — 6 checks

The hire-date proposal/approval flow and its effect on the algorithm:

| Check | What it proves |
| --- | --- |
| proposal does not count until approved | A self-proposed `hire_date_proposed` does **not** affect assignment (seniority stays 0 until approved). |
| admin approve makes it count | After approval, `hire_date` is set and the employee wins a contested favorite over a less-senior rival. |
| admin reject clears proposal | Rejecting clears `hire_date_proposed` (and `hire_date` is untouched). |
| admin direct edit clears pending | Editing the employee directly from the admin panel supersedes any pending proposal. |
| admin list shows pending proposal | The admin employees page renders the pending proposal with Approve/Reject actions. |
| old schema migration | Same as above — the suite's DB starts with the pre-migration schema. |

### 1.3 `tests/test_home_settings.py` — 10 tests / 11 checks

Home routing, the settings page, and the map:

| Check | What it proves |
| --- | --- |
| home → /map con perfil completo | `/` redirects to the map when name+surname+team are set. |
| home → /settings con perfil incompleto | `/` redirects to `/settings` when the profile is incomplete. |
| /profile → /settings | The old `/profile` URL still redirects to `/settings`. |
| /settings muestra formulario + logout | The settings page renders the profile form (action `/settings`) and a logout link. |
| nav en /map, nav en /calendar (×2) | Employee pages share the nav (links to `/map`, `/calendar`, `/settings`) — reported once per page, hence 11 checks from 10 tests. |
| mapa ofrece 'Plan your week' → /calendar | The map page offers the link to the week planner. |
| POST /settings (target primario del formulario) | Submitting the form redirects to `/map` (303). |
| mapa con paleta clara fija | The map uses the fixed light "paper" palette (`#f8f7f4`) and no Pico CSS variables — theme-independent legibility. |
| tema claro fijo (data-theme=light) | `base.html` renders `<html data-theme="light">`, so Pico 2 never follows the OS dark mode (light text on the light paper background would make titles invisible). |

### 1.4 `tests/test_desk_requests.py` — 12 checks

The "request this desk" / cede flow:

| Check | What it proves |
| --- | --- |
| peticion crea pending + email al ocupante | `POST /map/request` creates a `pending` row (requester, occupant, desk, day) and emails the occupant a `/requests/{id}` link. |
| pagina: ocupante ve Accept/Decline, peticionario ve estado | The occupant's view has the decision buttons; the requester's view shows the status without buttons. |
| aceptar: peticionario se sienta, ocupante reasignado | Accepting seats the requester at the requested desk and re-seats the occupant elsewhere (not waitlisted when a desk is free); the requester is emailed. |
| denegar: nadie se mueve, peticionario notificado | Declining keeps both employees exactly where they were and emails the requester. |
| peticion stale expira y notifica al peticionario | If the occupant leaves the office before deciding, the request expires (400 on decide) and the requester is notified. |
| no se puede pedir escritorio no elegible (helpdesk) | A non-helpdesk employee gets 400 requesting an occupied `HD-*` desk. |
| no se puede pedir el propio escritorio | Requesting your own desk → 400. |
| no se puede pedir escritorio libre | Requesting a free desk → 400. |
| tercero no ve ni decide la peticion | A third party gets 403 on both the view and the decide. |
| aceptar funciona con el dia bloqueado | Accepting still works when `is_day_locked` is forced true (mutual agreement bypasses the lock). |
| mapa: enlace de peticion en escritorio ajeno, no en el propio | The map renders the request form on the occupant's desk but not on the viewer's own desk. |
| migracion esquema antiguo | The suite's DB starts with the pre-migration schema; the new `deskrequest` table is created by `create_all` on startup. |

The suite is time-robust: after the 08:00 lock of a weekday, today is
locked and the window holds only 4 unlocked days, so the tests share days
instead of requiring 5 distinct ones (the "accept after the lock" check
simulates the lock by patching `is_day_locked`).

### 1.5 `tests/test_work_patterns.py` — 15 checks

Work pattern (hybrid / in-person) and the year-long vacation calendar:

| Check | What it proves |
| --- | --- |
| settings muestra work pattern (híbrido por defecto) | The settings form renders the `work_pattern` select with `hibrido` selected by default. |
| work_pattern=presencial persiste | `POST /settings` stores `presencial` on the employee row. |
| presencial auto-marcado al ver el mapa | Viewing the map auto-creates an in-office booking for a `presencial` employee. |
| presencial auto-marcado con escritorio | The auto-booking is seated by the normal resolver (`assigned` + a desk). |
| vacación marcada no la pisa el auto-marking | A pre-marked vacation is never overridden by the automatic booking. |
| híbrido->presencial convierte el remote a in office | Switching patterns converts future remote bookings to in office (and seats them). |
| remote bloqueado para presencial (400) | `POST /calendar/book` with `teletrabajo` is rejected for a `presencial` employee. |
| /vacation renderiza la cuadrícula del mes | The month grid renders for the current month. |
| toggle crea vacación fuera de la ventana | Toggling a weekday outside the 5-day window creates a `vacation` booking (it applies when the day reaches the window). |
| toggle off borra la vacación | Toggling again deletes the booking. |
| toggle rechaza fin de semana y pasado | Weekends and past days → 400. |
| calendario sin botón Vacation (In office/Remote quedan) | The week planner no longer offers a per-day Vacation button; the other two modes remain. |
| fila presencial: 'automatic' y sin botones | A `presencial` employee's day row shows "In office (automatic)" with no mode buttons. |
| calendario muestra 'On vacation' en el día marcado | A marked vacation day renders "On vacation" in the week planner. |
| mes fuera del año → 400, mes del año → 200 | Month navigation is clamped to the current year. |

---

## 2. Harness design

Each suite is self-contained and follows the same skeleton:

1. **Isolated database.** Before importing the app, the script sets
   `HOTDESK_DB` to a fresh temp file and unsets `SMTP_HOST` (console email
   mode — no network, no real mail).
2. **Old-schema pre-creation.** The temp file is populated with the
   *previous* production schema (no `password_hash`/`password_salt` on
   `employee`, no `purpose` on `magiclink`). When the app starts,
   `init_db` must run the column migration
   ([DATABASE.md](DATABASE.md) §5) — so the migration path is exercised on
   **every** run of **every** suite, not just in a dedicated test.
3. **Real app, real client.** `TestClient(app, follow_redirects=False,
   raise_server_exceptions=False)` — redirects are inspected explicitly
   (that's where the assertions live), and server exceptions surface as
   responses instead of crashing the suite.
4. **Token access.** Magic-link tokens are read straight from the
   `magiclink` table (the "email" is the console in dev mode; the tests
   take the shortcut).
5. **Reporting.** A module-level `RESULTS` list plus `report(name, ok,
   detail)` prints per-check output; the `__main__` block runs an explicit
   tuple of test functions, catches exceptions as failures, prints a
   summary, and exits 1 on any failure.

> **When you add a test function, add it to the `__main__` tuple.** The
> runner does not discover tests automatically — a defined-but-unlisted
> test silently never runs (this actually happened once; the password
> suite's three newest checks were defined but missing from the tuple until
> caught in review).

### What the suites deliberately don't do

- No unit tests of `assignment.py` in isolation — the algorithm is covered
  end-to-end (the seniority suite exercises contested favorites through the
  real HTTP + DB stack), which is the level at which bugs actually appear.
- No concurrency tests — the per-day locks and UNIQUE constraints are
  verified by construction ([ALGORITHM.md](ALGORITHM.md) §10) and by the
  bug-regression work that fixed the original races; a deterministic
  same-process race test would mostly test the test.
- No template visual tests beyond the palette check — the SVG map's layout
  is verified by eye.

---

## 3. How to add a test

1. Pick (or create) the suite that owns the behavior.
2. Write a `test_<thing>()` function using the existing helpers
   (`request_link`, `verify`, `set_password`, `register`, `admin_login`,
   `report`).
3. **Add it to the `__main__` tuple.**
4. Run the suite; the new check appears in the output and the count grows.

Style rules the suites follow:

- One behavior per test; the `report(...)` name is the human-readable
  assertion.
- Assertions on **status codes, redirect targets, cookies, and DB state** —
  not on HTML cosmetics (except where the behavior *is* the HTML, e.g. the
  palette or the nav).
- `client.cookies.clear()` between login-state changes (the helpers that
  log in as admin wipe the cookie jar, so re-login as admin after
  `client.cookies.clear()` if a test needs both).
- Each test uses its own email addresses so suites stay order-independent
  within a fresh database.

---

## 4. Known deferred items (documented, not tested)

These are accepted trade-offs for an internal app, listed so a future
reader knows they were considered:

- **No rate limiting** on `/login`, `/admin/login`, `/forgot`, or
  `/set-password`.
- **Non-atomic token consumption:** `verify_token` reads, then marks used;
  two truly parallel uses of the same token could both pass. Benign here
  (single process, 15-minute TTL, single-use in practice) but noted.
- **`tempfile.mktemp`** in the suites is deprecated; it's safe here because
  the file is created immediately by the same process, but a
  `tempfile.NamedTemporaryFile` would be the modern spelling.
- **`--forwarded-allow-ips "*"`** in the Dockerfile trusts `X-Forwarded-*`
  from any source; the mitigation is to bind port 8000 to loopback behind
  the proxy (see [DEPLOYMENT.md](DEPLOYMENT.md) §6.6).
