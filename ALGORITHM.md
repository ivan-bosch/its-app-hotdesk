# Desk assignment algorithm

How Hotdesk ITS decides who sits where, and when. The code lives in
`app/assignment.py`; this document is the prose walkthrough.

## The big idea: instant full recalculation

There is **no daily batch job and no "pending" state**. Desks are assigned
the moment someone declares "in office", and **every change re-plans the
whole day from scratch** for everyone who declared presencial that day:

1. Someone marks a day as "In office" (or switches to remote/vacation,
   freeing a desk).
2. `resolve_day()` immediately recomputes *all* presencial assignments for
   that day with full information about everyone's preferences.
3. Anyone who entered or left the waitlist gets an email.

Because the algorithm is **deterministic** (same declarations in → same
assignment out), re-running it on every change is safe and cheap: with ~15
desks and a few dozen employees, a full recalc is a handful of SQL queries
and a sort.

### Why not a daily batch?

An earlier version collected everyone's intent until a 07:00 cutoff and
resolved it all at once. That optimized for "best global assignment with
full information", but in practice it meant you didn't know your desk until
the morning itself, and the pending/resolved state machine (plus the
scheduler and its lazy safety net) was the most complex part of the app.
Instant recalc gives immediate feedback — you see your desk the second you
declare — at the cost of occasionally moving someone when a later
declaration changes the picture.

## The day lock

Each day **locks at `BOOKING_LOCK_HOUR:00`** (08:00 by default,
server-local time, on the day itself). From that moment, employees can no
longer change that day's choice — in office, remote, and vacation are all
frozen. Future days stay open.

The lock exists so that morning-of recalculations don't pull the rug out
from under people who are already on their way in. Admins can still
override assignments after the lock (see [Admin overrides](#admin-overrides)).

## `resolve_day()`: the assignment engine

Called after every booking change for a day. Decision order, per employee
declaring presencial:

### Step 1 — Boss

The boss (`Employee.is_boss`) always gets `DESP-01`. Nobody else is eligible
for it.

### Step 2 — Favorite desks, contested ones resolved by seniority

Everyone with a `favorite_desk_id` that's free and they're eligible for is
a candidate for it. If **two or more** employees want the same favorite,
whoever has more **seniority** wins it outright — a deterministic
tie-break, no dice roll:

```
seniority = (today - hire_date).days / 365.25   # 0 if no hire_date
```

Ties (same seniority) are broken by whoever declared first
(`Booking.created_at`). Losers fall through to step 3.

This is where **seniority displaces**: if Ana (2 years) is sitting on P01
and Bob (15 years) declares P01 as his favorite, the next recalc gives P01
to Bob and reseats Ana via step 3. She is **never** pushed to the waitlist
just because someone more senior showed up — only a genuinely full office
does that.

### Step 3 — Team-clustering fallback

Everyone left is processed in **arrival order** (`Booking.created_at` —
who declared first). Each gets:

1. The free eligible desk **nearest to an already-seated teammate** (from
   this same recalc pass), measured by straight-line distance between the
   desks' x/y map coordinates.
2. If no teammate is seated yet: the free eligible desk **nearest to their
   own favorite** (even though the favorite itself is taken) — you still
   end up in the neighborhood you like.
3. If they have no favorite either: the **lowest free desk code**
   (deterministic, stable).

Processing order is arrival order, *not* sorted by team — employees with
no team (`team_id = NULL`) must not jump ahead of real teams.

### Step 4 — Waitlist

If no eligible free desk remains, the booking is stored as
`status = waitlisted`, `desk_id = None`, and the employee is emailed
immediately. They're automatically picked up the moment a desk frees up
(see below).

## What happens when a desk frees up

Two paths, depending on who freed it:

- **An employee** switches to remote or vacation → `book_day` frees their
  desk and runs a **full `resolve_day`** for that day. The freed desk (and
  everything else) is re-planned: favorites are re-honored, and the waitlist
  is drained in the process.
- **An admin** reassigns someone → `admin_reassign` bumps the current
  holder to the waitlist and runs `promote_waitlist`, which retries *only*
  the waitlisted bookings (in arrival order) against whatever's free —
  everyone else keeps their desk. Admin overrides are surgical; they never
  reshuffle the whole office.

## Eligibility rules

| Desk | Who can get it |
|---|---|
| `DESP-01` | Only the boss (`Reserved.boss`) |
| `HD-01/02/03` | Only members of the team flagged `is_helpdesk` (`Reserved.helpdesk`) |
| Everything else | Anyone (`Reserved.none`) |

The boss's desk is also excluded from the step-3 fallback for non-bosses,
so it stays free for the boss even if they haven't declared yet.

## Email notifications

Two events send a best-effort email (via `app/email.py::send_email` —
real SMTP if configured, console fallback otherwise; delivery failures are
logged, never fatal):

1. **You've been waitlisted** — your presencial declaration couldn't be
   seated (office full), or an admin bumped you with nowhere else to go.
2. **A desk opened up for you** — you were waitlisted and a recalc or
   promotion just seated you. Includes the desk code.

Desk-to-desk moves (e.g. someone more senior claimed your favorite) are
deliberately silent — you see your current desk when you open the app. And
your *own* click never emails you either: declaring "in office" and getting
a desk straight away is visible on screen, no email needed.

## Vacation mode

`Mode.vacation` is **functionally identical to `teletrabajo`** (remote): no
desk is ever assigned, and switching to it frees any desk you hold that day
(triggering a full recalc). It exists as a separate value purely for
labeling — the map view shows "on vacation" apart from "working remotely".

## Map view rosters

`/map` shows the floor plan plus, for everyone not at a desk box:

| Bucket | Meaning |
|---|---|
| **Remote** | `mode = teletrabajo` |
| **On vacation** | `mode = vacation` |
| **Waitlisted, no desk** | `mode = presencial`, `status = waitlisted` |
| **Unknown** | No booking at all for that day |

Every employee appears in exactly one place: a desk box, or one bucket.

## Admin overrides

`admin_reassign()` (used by `/admin/reassign`) forces an employee onto a
specific desk (or the waitlist) for a day, bypassing all rules **and** the
day lock. The previous holder of that desk (if any) is bumped to the
waitlist and immediately re-evaluated by `promote_waitlist`.

## Race conditions

Two people can click at the same time. The `Booking` table has two unique
constraints — `(day, desk_id)` and `(employee_id, day)` — so a race becomes
an `IntegrityError` on commit, not a silent double-booking. `book_day`
retries a bounded number of times against fresh state. Inside
`resolve_day`, all desks are freed (`desk_id = NULL`) before re-assigning,
so two bookings swapping desks within one transaction can't trip the
`(day, desk_id)` constraint.

## What the algorithm deliberately does NOT do

- **No daily batch / scheduler** — assignment is instant, on every change.
- **No probabilistic seniority** — seniority is a deterministic tie-break
  for contested favorites, nothing else. It doesn't weight the fallback.
- **No waitlist-by-seniority** — the waitlist is strictly arrival order.
- **No cross-day optimization** — each day is independent.
- **No half-days or time slots** — a booking is for the whole day.
