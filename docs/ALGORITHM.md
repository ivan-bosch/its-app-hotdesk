# Desk Assignment Algorithm

How Hotdesk ITS decides who sits where, and when. The code lives in
`app/assignment.py`; this document is the full technical walkthrough: the
model, the exact decision procedure (with pseudocode), worked examples, the
concurrency design, and the invariants the system maintains.

---

## 1. The big idea: instant full recalculation

There is **no daily batch job, no scheduler, and no "pending" state**. Desks
are assigned the moment someone declares "in office", and **every change
re-plans the whole day from scratch** for everyone who declared presencial
that day:

1. Someone marks a day as "In office" (or switches to remote/vacation,
   freeing a desk).
2. `resolve_day()` immediately recomputes *all* presencial assignments for
   that day, with full information about everyone's preferences.
3. Anyone who entered or left the waitlist gets an email.

Because the algorithm is **deterministic** (same set of declarations in →
same assignment out), re-running it on every change is safe and cheap: with
15 desks and a few dozen employees, a full recalc is a handful of SQL
queries and a sort (see §14 for complexity).

### Why not a daily batch?

An earlier version collected everyone's intent until a 07:00 cutoff and
resolved it all at once. That optimized for "best global assignment with
full information", but in practice it meant you didn't know your desk until
the morning itself, and the pending/resolved state machine (plus the
scheduler and its lazy safety net) was the most complex part of the app.
Instant recalc gives immediate feedback — you see your desk the second you
declare — at the cost of occasionally moving someone when a later
declaration changes the picture. Desk-to-desk moves are deliberately silent
(§8); only waitlist transitions email.

---

## 2. The day lock

Each day **locks at `BOOKING_LOCK_HOUR:00`** (08:00 by default,
server-local time, on the day itself):

```python
lock_datetime(day) = datetime.combine(day, time(hour=BOOKING_LOCK_HOUR))
is_day_locked(day) = datetime.now() >= lock_datetime(day)
```

From that moment, employees can no longer change that day's choice — in
office, remote, and vacation are all frozen (`book_day` raises
`DayLockedError` → HTTP 403). Future days stay open. Admins can still
override assignments after the lock (§7).

Two deliberate exemptions: accepting a desk request bypasses the lock (a
cede is a mutual agreement — see the desk-requests routes), and the
in-person auto-booking of §9 bypasses it too (a standing declaration, not a
last-minute change).

The lock exists so that morning-of recalculations don't pull the rug out
from under people who are already on their way in. Note the asymmetry: the
lock freezes *employee* choices, not the *data* — an admin reassignment on a
locked day still triggers a re-plan for everyone else.

---

## 3. Eligibility rules

| Desk | Who can be seated there |
| --- | --- |
| `DESP-01` | Only the boss (`Employee.is_boss`) |
| `HD-01/02/03` | Only members of the team flagged `is_helpdesk` |
| Everything else | Anyone |

Eligibility is checked per (desk, employee) pair by `_is_eligible`. Two
extra rules:

- The boss's desk is **excluded from the step-3 fallback for non-bosses**
  (`d.code != BOSS_DESK_CODE`), so it stays free for the boss even if the
  boss hasn't declared yet.
- A non-helpdesk employee who picks an `HD-*` desk as their favorite simply
  never matches it (eligibility fails in step 2); the settings form
  silently clears such a choice on submit.

---

## 4. `resolve_day()`: the assignment engine

`resolve_day(session, day)` is called after every booking change that
affects seating. It runs under the per-day lock (§11) and recomputes every
presencial assignment for `day` from scratch.

### Pseudocode

```
resolve_day(day):
    presencial = all bookings for `day` with mode == presencial
    if presencial is empty: return

    snapshot previous_status[employee] for each booking   # for email diffs

    # Free every desk first: the UNIQUE(day, desk_id) constraint would
    # reject two bookings swapping desks within one commit otherwise.
    for b in presencial: b.desk_id = None; b.status = None
    flush()

    assigned_desk_of = {}          # desk_id -> employee_id (this pass only)

    # STEP 1 — Boss
    for each remaining employee who is_boss:
        seat them at DESP-01 if free

    # STEP 2 — Favorite desks; contested ones go to the most senior
    wanters = {}                   # favorite_desk_id -> [employee_ids]
    for each remaining employee with a free, eligible favorite:
        wanters[fav].append(employee)
    for desk_id, candidates in wanters:
        if desk_id still free:
            winner = max(candidates, key = (
                seniority_score(emp),          # years since hire_date, 0 if unset
                -booking.created_at.timestamp(),# tie: who declared first
                emp.id                          # last resort: deterministic
            ))
            seat winner at desk_id
            # losers stay "remaining" for step 3

    # STEP 3 — Team-clustering fallback, in arrival order
    for each remaining employee, sorted by (booking.created_at, emp.id):
        eligible_free = free desks they're eligible for, minus DESP-01
        if eligible_free is empty:
            b.status = waitlisted; continue
        teammate_desks = desks held (this pass) by seated same-team members
        if teammate_desks:
            best = argmin over eligible_free of (min distance to a teammate desk, code)
        elif employee has a favorite:
            best = argmin over eligible_free of (distance to favorite, code)
        else:
            best = argmin over eligible_free of code
        seat employee at best

    commit()

    # STEP 4 — Emails (after the commit, best-effort)
    for each booking whose status changed:
        if now waitlisted:                          notify_waitlisted
        if waitlisted -> assigned:                  notify_assigned_from_waitlist
        # desk-to-desk moves and first-time assignments: silent
```

### Step 1 — Boss

The boss (`Employee.is_boss`) always gets `DESP-01`. Nobody else is eligible
for it. (If the boss didn't declare, the desk simply stays free — it is
excluded from everyone else's candidate set.)

### Step 2 — Favorite desks, contested ones resolved by seniority

Everyone whose `favorite_desk_id` is free and they're eligible for it is a
candidate. If **two or more** employees want the same favorite, whoever has
more **seniority** wins it outright — a deterministic tie-break, no dice
roll:

```
seniority_score(emp) = max(0, (today - emp.hire_date).days / 365.25)   # 0 if unset
```

Ties (same seniority) go to whoever declared first (`Booking.created_at`);
an exact tie on that (same-second declarations) falls back to the higher
employee id, purely to keep the winner deterministic. Losers fall through to
step 3 — they are **never** pushed to the waitlist just for losing a
favorite.

This is where **seniority displaces**: if Ana (2 years) is sitting on P01
and Bob (15 years) declares P01 as his favorite, the next recalc gives P01
to Bob and reseats Ana via step 3. She is only waitlisted if *literally no*
eligible desk remains.

### Step 3 — Team-clustering fallback

Everyone left is processed in **arrival order** (`Booking.created_at`, then
employee id — who declared first). Each gets, in priority order:

1. The free eligible desk **nearest to an already-seated teammate** (from
   this same recalc pass), measured by straight-line Euclidean distance
   between the desks' x/y map coordinates.
2. If no teammate is seated yet: the free eligible desk **nearest to their
   own (lost) favorite** — you still end up in the neighborhood you like.
3. If they have no favorite either: the **lowest free desk code**
   (deterministic, stable).

Every `argmin` breaks distance ties by desk code, so the result is fully
deterministic. Processing order is arrival order, *not* sorted by team —
employees with no team (`team_id = NULL`) must not jump ahead of real teams,
and team-less employees have no `teammate_desks`, so they use rule 2 or 3.

### Step 4 — Waitlist

If no eligible free desk remains, the booking is stored as
`status = waitlisted`, `desk_id = NULL`, and the employee is emailed
immediately (after the commit). They're picked up automatically the moment a
desk frees up — either by a full `resolve_day` (employee-side change) or by
`promote_waitlist` (admin-side change), §5.

---

## 5. What happens when a desk frees up

Two paths, depending on who freed it — and they are deliberately different:

### Employee-side: full re-plan

An employee switches to remote or vacation → `book_day` frees their desk and
runs a **full `resolve_day`** for that day. The freed desk (and everything
else) is re-planned: favorites are re-honored, team clustering re-runs, and
the waitlist is drained in the process (waitlisted employees are presencial
bookings, so they're part of the re-resolution and get seated in step 3 if a
desk is free).

### Admin-side: surgical promotion

An admin reassigns someone (or an employee is deleted) → `admin_reassign` /
the delete handler bumps the affected booking(s) to the waitlist and runs
`promote_waitlist`, which retries **only the waitlisted bookings** (in
arrival order) against whatever's free, using the greedy single-employee
`assign_desk` (§6). Everyone else keeps their desk. Admin overrides are
surgical; they never reshuffle the whole office.

---

## 6. `assign_desk`: the greedy single-employee placer

`assign_desk(session, booking, employee)` mutates one booking in place. It
is **not** used by the normal flow (that goes through `resolve_day`); it
exists only for `promote_waitlist` and `admin_reassign`, where re-planning
the whole day would be wrong. Decision order:

1. Boss → `DESP-01` if free.
2. Favorite desk, if free and eligible.
3. Nearest free eligible desk to a teammate already seated that day (from
   the *committed* database state, not a recalc pass).
4. Else nearest free eligible desk to their (lost) favorite.
5. Else lowest free desk code.
6. Else → waitlisted.

Like step 3 of `resolve_day`, it excludes the boss's desk from non-boss
candidates and breaks distance ties by desk code.

---

## 7. Admin overrides

`admin_reassign(session, employee, day, desk_id)` (used by
`/admin/reassign`) forces an employee onto a specific desk — or to the
waitlist when `desk_id` is `None` — for a day, bypassing all eligibility
rules **and** the day lock. If another employee currently holds that desk
that day, they are bumped to the waitlist and immediately re-evaluated by
`promote_waitlist` — they may land on another free desk rather than staying
stuck. Email consequences:

- The bumped employee gets "a desk opened up for you" if promotion seats
  them, or "no desk available" if they remain waitlisted.
- The forced employee gets no email (admin actions are visible on the map).

Deleting an employee (`/admin/employees/{id}/delete`) removes their
bookings and then runs `promote_waitlist` for every affected day, so a freed
desk doesn't sit empty.

**Desk requests reuse this path.** When an employee "requests this desk"
from the map and the occupant accepts (`/requests/{id}/decide`), the app
calls `admin_reassign(requester, day, desk)` — the requester is seated at
the desk and the occupant is bumped and re-seated surgically, exactly like
an admin override. That is also why accepting works **after the day lock**:
a cede is a mutual agreement between two people who are both affected, the
same semantics as an admin override. See [API.md](API.md) §Desk requests
for the full flow (email, decision page, staleness).

---

## 8. Email notifications

Two events send a best-effort email (via `app/email.py::send_email` — real
SMTP if configured, console fallback otherwise; delivery failures are
logged, never fatal, `raise_on_error=False`):

1. **"Hotdesk: no desk available"** — your presencial declaration couldn't
   be seated (office full), or an admin bumped you with nowhere else to go.
2. **"Hotdesk: a desk opened up for you"** — you were waitlisted and a recalc
   or promotion just seated you. Includes the desk code and a `/map?day=…`
   link.

The trigger is a **status transition** detected by diffing the pre-recalc
snapshot against the post-recalc state:

| Old status | New status | Email |
| --- | --- | --- |
| anything | `waitlisted` | "no desk available" |
| `waitlisted` | `assigned` | "a desk opened up" |
| `assigned` | `assigned` (different desk) | **silent** |
| `NULL` (first declaration) | `assigned` | **silent** — it's your own click, visible on screen |

Desk-to-desk moves (e.g. someone more senior claimed your favorite) are
deliberately silent: you see your current desk when you open the app.

---

## 9. In-person employees (work pattern)

`Employee.work_pattern` is a standing declaration about *how* someone works,
separate from the per-day `Booking.mode`:

| Pattern | Meaning |
| --- | --- |
| `hibrido` (default) | Chooses in office / remote per day in the calendar, as before. |
| `presencial` | In the office every working day (the usual case for Help Desk and interns). No remote option, no daily action. |

### Auto-booking (`ensure_present_bookings`)

Every render of a day — the employee's own calendar, the map, the admin
dashboard, the admin reassign view, and every booking that triggers a
re-plan — first runs `ensure_present_bookings(session, day)`:

1. Select all employees with `work_pattern = presencial` and a complete
   profile (name + surname + team; without a team there is nothing to seat
   them near).
2. For each one without a booking for `day`, create a `presencial` booking.
3. If anything was created, run one `resolve_day(day)` — the automatic
   bookings are seated by the exact same rules as a manual "in office"
   click.

Consequences:

- An in-person employee shows up on the map, the roster and the admin
  dashboard **without ever opening the app** — the common help-desk case.
- A pre-marked **vacation wins over the automatic booking**: the helper
  skips anyone already booked that day, so a `vacation` booking is never
  overridden (and unmarking a vacation day drops the automatic booking
  straight back in).
- **Lock exemption.** The auto-booking bypasses the day lock on purpose: it
  is a standing declaration, not a last-minute change. Without the
  exemption an in-person employee who never opens the app would never get a
  desk, because their first booking would always arrive after 08:00. The
  same principle applies to switching *to* `presencial` from the settings
  form: future `teletrabajo` bookings are converted to `presencial` and
  those days re-plan, even if locked.
- `POST /calendar/book` with `mode=teletrabajo` is rejected with **400**
  for in-person employees (defense in depth — their UI has no such button).
- Switching *back* to `hibrido` undoes nothing: existing in-office bookings
  stay, and the auto-booking simply stops for future days.

---

## 10. Vacation mode

`Mode.vacation` is **functionally identical to `teletrabajo`** (remote): no
desk is ever assigned, and switching to it frees any desk you hold that day
(triggering a full recalc, same as switching to remote). It exists as a
separate value purely for labeling — the map view shows "on vacation" apart
from "working remotely".

Vacation days are managed on the **year-long calendar page**
(`/vacation?month=YYYY-MM`, see [API.md](API.md) §Vacation), not per day in
the week planner: the per-day Vacation button is gone. A mark is a `vacation`
booking row, and marks outside the 5-day booking window are stored as-is —
they simply apply when the day reaches the window, because the map, the
resolver and the admin views all read the same rows.

### Map view rosters

`/map` shows the floor plan plus, for everyone not at a desk box
(`mapview.py::build_map`):

| Bucket | Meaning |
| --- | --- |
| **Remote** | `mode = teletrabajo` |
| **On vacation** | `mode = vacation` |
| **Waitlisted, no desk** | `mode = presencial`, `status != assigned` (waitlisted — or a stuck `NULL`, which is shown rather than dropped) |
| **Unknown** | No booking at all for that day |

Every employee appears in exactly one place: a desk box, or one bucket.

---

## 11. Race conditions and concurrency

Two people can click at the same time. Three layers keep that from ending
in two people assigned to the same desk:

### Layer 1 — per-day re-entrant locks

Every same-day mutation — `book_day`, `resolve_day`, `promote_waitlist`,
`admin_reassign` — runs under a `threading.RLock` keyed by the date
(`_day_lock`). `RLock` (not `Lock`) because a high-level function and the
resolver it call both acquire the same day's lock. Different days get
different locks and never block each other.

Without this layer, two concurrent `book_day` calls for the same day would
each run `resolve_day` on an overlapping-but-stale snapshot and could
double-book a desk (the `UNIQUE(day, desk_id)` constraint would then reject
the second commit with an `IntegrityError` → HTTP 500) or leave a booking
stuck with no status.

### Layer 2 — database unique constraints

`Booking` carries `UNIQUE(day, desk_id)` and `UNIQUE(employee_id, day)`. A
race that slips past the locks becomes an `IntegrityError` on commit, not a
silent double-booking. `book_day` catches it, rolls back, and retries up to
`MAX_ASSIGN_RETRIES` (3) times, re-reading fresh state each attempt — a
losing party falls through to the next candidate. If there's still no room
after the retries, the booking is recorded as waitlisted (or a 503 "please
retry" is returned if it couldn't be recorded at all).

### Layer 3 — transaction hygiene inside the resolver

- `resolve_day` **frees every desk first** (`desk_id = NULL` for all
  presencial bookings, flushed) before re-assigning. Two bookings swapping
  desks within one commit would otherwise trip `UNIQUE(day, desk_id)`
  mid-transaction.
- `book_day` calls `session.expire_all()` before `resolve_day`, so the
  resolver reads committed state, not stale in-memory objects.
- `book_day` refreshes `booking.created_at` on each retry attempt, so a
  retried booking's arrival order reflects when it was actually recorded.

**Scope note:** these locks are in-process. They serialize threads of *one*
uvicorn process; they do not coordinate across multiple workers or
containers. Run one process (see [ARCHITECTURE.md](ARCHITECTURE.md) §6 and
§10).

---

## 12. Worked examples

Desk coordinates from the seed (top row: P01 (200,50), P02 (280,50),
P03 (360,50); "by the office" row: P07 (200,250), P08 (280,250)).

### Example A — a contested favorite, seniority decides

- Ana (team Data, `hire_date` 2023-01-01 → ~3 years) has favorite **P01**
  and declares Monday in office at 09:00.
- Bob (team Data, `hire_date` 2008-06-01 → ~18 years) has favorite **P01**
  and declares Monday in office at 09:30.

Monday's `resolve_day` (triggered by Bob's declaration):

1. No boss declared.
2. Step 2: both want P01. Bob's seniority (18) beats Ana's (3) → **Bob gets
   P01**. Ana stays "remaining".
3. Step 3 (arrival order): Ana is processed. Her teammate Bob is seated at
   P01 → nearest free desk to P01 is **P02** (distance 80) → Ana gets P02.

Later, Bob switches to remote. `book_day` frees P01 and re-resolves: Ana's
favorite P01 is now uncontested → **Ana gets P01**. Her desk-to-desk move is
silent; Bob's freed desk was visible to him the moment he clicked.

### Example B — team clustering with no favorites

Three Security members declare Tuesday, none has a favorite: Carla 08:10,
Diego 08:20, Elsa 08:30.

1. Step 2: no favorites, skipped.
2. Step 3, arrival order:
   - Carla: no teammate seated yet, no favorite → lowest free desk code →
     **P01**.
   - Diego: teammate Carla seated at P01 → nearest free desk to P01 →
     **P02**.
   - Elsa: teammates at P01 and P02 → nearest free desk → **P03**.

Result: the Security pod occupies the top row, seated in arrival order. If a
fourth Security member declares later, they land on P04 (nearest to the
pod) — the cluster grows coherently.

### Example C — waitlist and promotion

15 desks total (14 general + the boss's). Suppose 14 non-boss employees
declare Wednesday and are all seated; a 15th non-boss, Frank, declares:

1. Step 3: no eligible free desk remains → Frank is **waitlisted** and
   immediately emailed "no desk available".

Greta (seated at P06) switches to remote. `book_day` re-resolves the day:
Frank is a presencial booking, so he's part of the re-resolution — step 3
seats him at the nearest free desk to his teammates (or his favorite's
neighborhood, or the lowest code) → Frank gets a desk and is emailed "a desk
opened up for you".

### Example D — admin override with a bump

Marta is seated at P05 on Thursday. An admin forces Carlos (no favorite)
onto P05:

1. `_admin_reassign`: Marta's booking is bumped — `desk_id = NULL`,
   `status = waitlisted`. Carlos's booking is set to P05, `assigned`.
   Commit.
2. `promote_waitlist(Thursday)`: only waitlisted bookings are retried, in
   arrival order. `assign_desk` for Marta: favorite if free, else nearest
   to a seated teammate, else lowest code. Suppose P04 is free and nearest
   to her team → **Marta gets P04** and is emailed "a desk opened up for
   you".
3. If no desk had been free, Marta would remain waitlisted and be emailed
   "no desk available" instead.

Nobody else moved: the override was surgical.

---

## 13. Invariants

The system maintains these at all times (enforced by the combination of the
algorithm and the DB constraints):

1. **No double-booking:** at most one `assigned` booking per `(day, desk)`
   — `UNIQUE(day, desk_id)`.
2. **One booking per person per day:** `UNIQUE(employee_id, day)`.
3. **Resolved state:** after `resolve_day`, every presencial booking is
   either `assigned` (with a `desk_id`) or `waitlisted` (`desk_id = NULL`).
4. **Reserved desks stay reserved:** `DESP-01` is only ever held by the
   boss; `HD-*` only by the `is_helpdesk` team (eligibility is checked in
   every seating step).
5. **Determinism:** the same set of bookings (including `created_at`)
   always produces the same assignment. There is no randomness anywhere in
   the algorithm.
6. **Seniority is scoped:** it only decides contested favorites in step 2.
   It plays no role in team clustering, late arrivals, or waitlist
   promotion, all of which are purely arrival-order based.
 7. **Arrival order is preserved:** waitlist promotion and step-3 processing
    both walk `created_at` ascending.
 8. **In-person means in office:** a `work_pattern = presencial` employee who
    has a complete profile never has a `teletrabajo` booking, and — once any
    view or booking touches a day — has a `presencial` booking for it
    unless they marked that day as `vacation` (vacation wins over the
    automatic booking).

---

## 14. Complexity

Per `resolve_day` invocation, with `E` presencial declarations and `D` desks
(15 in the seed):

- Loading bookings/desks/employees: `O(E + D)` queries.
- Step 2: `O(E)` eligibility checks + `O(E log E)` total for the per-desk
  winner selection.
- Step 3: `O(E)` employees × `O(D)` candidate desks × `O(E)` teammate
  distance scan = `O(E² · D)` distance computations in the worst case —
  with `E ≈ 30` and `D = 15` that is a few thousand float operations,
  i.e. microseconds.
- One commit per recalc.

This is why "re-plan the whole day on every change" is free at this scale.
The algorithm would stop being cheap around the hundreds of desks /
hundreds of employees; at that point a batch or a proper matching solver
would be the next step (see §15).

---

## 15. What the algorithm deliberately does NOT do

- **No daily batch / scheduler** — assignment is instant, on every change.
- **No probabilistic seniority** — an earlier version had a capped-% chance
  for a senior employee to retroactively "reclaim" a favorite desk from
  whoever had booked it first. That still depended on booking order (there
  had to be a "first" to steal from) and added randomness. Now that a whole
  day's declarations are resolved together, there's no "first": every
  contested favorite is compared once, and seniority decides outright.
- **No waitlist-by-seniority** — the waitlist is strictly arrival order.
- **No general seniority priority queue** — seniority only decides
  contested favorites; it doesn't weight the fallback or promotion.
- **No fixed zones per team** — an earlier version tried "zone inferred from
  majority of favorites", but it didn't match the real office (an open
  area, not separate rooms) and was replaced with direct physical distance.
- **No limit on remote/in-office days** — bookings can be made up to 1 week
  ahead with no minimum or maximum in-office days (an explicit decision).
- **No cross-day optimization** — each day is independent.
- **No half-days or time slots** — a booking is for the whole day.
- **No combinatorial matching solver** — each employee has at most one
  favorite (not a ranked list) and there are only 15 desks; resolving
  favorite conflicts one at a time by seniority, then falling back to simple
  proximity, was a deliberate scope decision over something like the
  Hungarian algorithm, which would be overkill here.
