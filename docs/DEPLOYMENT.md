# Deployment

How to install, run, configure, and operate Hotdesk ITS — from local
development to a Docker deployment behind a reverse proxy. For the TLS layer
itself (Caddy/nginx/certificates), see [HTTPS.md](HTTPS.md); for what the
configuration variables *do* to the app, see
[ARCHITECTURE.md](ARCHITECTURE.md) §9.

---

## 1. Prerequisites

- **Python 3.11** (pinned in `.python-version`) — for local development.
  Docker deployments don't need Python on the host.
- **[`uv`](https://docs.astral.sh/uv/)** for dependency management. The repo
  ships a `uv.lock` with exact versions. If you don't have `uv`:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

- For Docker deployments: **Docker** with the **Compose** plugin.

---

## 2. Install dependencies (local development)

From the project root:

```bash
uv sync
```

This creates/updates `.venv/` with exactly what `uv.lock` pins (FastAPI,
SQLModel, uvicorn, itsdangerous, python-multipart, Jinja2).

---

## 3. Run locally (no real email)

```bash
uv run uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000`. On the **first** start:

- `hotdesk.db` (SQLite, in `.gitignore`) is created in the project root and
  seeded: 15 desks from the real floor plan, 4 zones, 5 default teams
  (Data, SciCom, AI, Security, Help Desk), and a default admin account.
- The admin credentials are printed **once** to the server console:

  ```
  --- ADMIN ACCOUNT CREATED ---
  username: admin
  password: admin123
  Change this password from /admin/password after logging in.
  ---
  ```

In this mode (no `SMTP_HOST`), employee magic links are **not** emailed —
they are printed to the server console, exactly like the admin password.
That is the default behavior, designed so the whole login flow works
end-to-end in development without any mail server.

### Recommended first steps

1. Log in at `http://localhost:8000/admin/login` with `admin` / `admin123`.
2. Go to `/admin/password` and change the password immediately.
3. Review `/admin/teams` and `/admin/employees`: either add real people, or
   let each person self-register via `/login` (the `Employee` row is created
   automatically when they verify their first magic link).

---

## 4. Configure real email (SMTP)

Without `SMTP_HOST`, `send_email` prints to the console. To send real email,
set the `SMTP_*` variables (full table in
[ARCHITECTURE.md](ARCHITECTURE.md) §9):

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="hotdesk@irbbarcelona.org"    # sending account
export SMTP_PASSWORD="xxxxxxxxxxxxxxxx"        # app password, 16 chars, no spaces
export SMTP_FROM="hotdesk@irbbarcelona.org"    # optional; defaults to SMTP_USER
export SMTP_USE_TLS="true"                     # optional; Gmail requires TLS (already the default)
```

Also set a real session secret (the default is an insecure dev value):

```bash
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

Keep these out of the repo. The app does **not** auto-load `.env` files —
it reads the process environment only, so however you deploy (shell exports,
systemd `EnvironmentFile`, Docker `env_file`/`environment`) must put the
variables into the process environment.

### 4.1 Gmail / Google Workspace app password

Gmail refuses the normal account password for SMTP when two-step
verification is on (mandatory on Google Workspace accounts such as IRB's):

1. Sign in to the Google account that will send mail.
2. Go to **myaccount.google.com/security**.
3. Enable **2-Step Verification** if it isn't already.
4. Open **App passwords** (myaccount.google.com/apppasswords).
5. Create one (suggested name: "Hotdesk SMTP"). Google shows a 16-character
   password once — copy it.

> On **Google Workspace** domains, an administrator may have to enable app
> passwords (or "less secure app access") at the organization level before
> step 4 is available. If the option is missing, contact the institution's
> Google Workspace admin.

### 4.2 Office 365 / institutional relay

- **Office 365:** `SMTP_HOST=smtp.office365.com`, port `587`, STARTTLS,
  `SMTP_USER`/`SMTP_PASSWORD` = the account credentials. Note that modern
  Exchange Online may require OAuth2 client credentials instead of basic
  auth; if basic auth is disabled for the tenant, use an institutional relay
  or a dedicated sending app.
- **Open relay:** point `SMTP_HOST` at the relay, leave `SMTP_USER`/
  `SMTP_PASSWORD` unset (login is attempted only when *both* are set), and
  set `SMTP_FROM` to the address the relay will accept.

### 4.3 SMTP troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `(535, '5.7.8 Username and Password not accepted')` | Wrong/expired app password, or 2SV not enabled on the sending account. |
| Timeout / connection refused | Port 587 blocked by the firewall of the network the app runs on. |
| Sent but not delivered | Check spam. Gmail limits: 500 mail/day (regular) / 2000 (Workspace) — far above this app's needs. |
| Need to debug without risking real sends | Unset `SMTP_HOST` temporarily → console mode. |

A failed magic-link send returns HTTP 500 (the "check your email" page would
otherwise lie); failed waitlist notifications are logged and ignored
(best-effort). See [ARCHITECTURE.md](ARCHITECTURE.md) §8.

---

## 5. The daily booking lock (`BOOKING_LOCK_HOUR`)

Assignment is **instant** — there is no scheduled job and no "pending" state
(see [ALGORITHM.md](ALGORITHM.md)). The only time-related configuration is
the **cutoff hour**: from `BOOKING_LOCK_HOUR:00` on the day itself
(server-local time), that day stops accepting employee changes; future days
stay open, and admins can still override from `/admin/reassign`.

- Default: `8` (08:00). Integer hours 0–23 only:

  ```bash
  export BOOKING_LOCK_HOUR="9"   # days close at 09:00 server time
  ```

- **Timezone matters:** the cutoff uses the server's local time
  (`datetime.now()`), not UTC. If the deployment host runs a different
  timezone than the Barcelona office, set the host/container timezone
  correctly (Docker does this via `TZ=Europe/Madrid`) or compensate by
  adjusting the hour.

---

## 6. Docker deployment

The repo ships `Dockerfile`, `docker-compose.yml`, and `.env.example`.

### 6.1 What the image is

- Base `python:3.11-slim`; dependencies installed with `uv sync --frozen
  --no-dev` from `uv.lock` (exact locked versions, no dev deps).
- Runs as a **non-root user** `hotdesk` (uid **10001**).
- `TZ=Europe/Madrid` and `HOTDESK_DB=/data/hotdesk.db` are baked in.
- `CMD` runs uvicorn with `--proxy-headers --forwarded-allow-ips "*"` so the
  app honors `X-Forwarded-Proto`/`X-Forwarded-For` from a reverse proxy —
  this is what makes magic links come out as `https://…` and session cookies
  get the `Secure` flag when served over TLS (see [HTTPS.md](HTTPS.md)).

### 6.2 Where the database lives

The SQLite file lives in a **host directory** next to `docker-compose.yml`:
the compose file bind-mounts `./data:/data`, so the database is
`<repo>/data/hotdesk.db` on the host. It is a plain file: visible directly,
and the backup is a copy of it (with the container stopped).

### 6.3 Prepare the environment

```bash
cp .env.example .env
```

Fill in `.env`:

- `SECRET_KEY` (**required** — compose refuses to start without it):
  `python3 -c 'import secrets; print(secrets.token_hex(32))'`
- The `SMTP_*` variables only if you want real email (§4). Without
  `SMTP_HOST`, magic links are printed to the container console
  (`docker compose logs`) instead of being sent.
- `BOOKING_LOCK_HOUR` if you don't want the 08:00 default.

### 6.4 Start

```bash
mkdir -p data && chown 10001:10001 data   # the container writes as uid 10001
docker compose up --build -d
```

> **The `chown` matters.** If `data/` doesn't exist, the Docker daemon
> creates it as `root:root`, and the container (running non-root) cannot
> create the database inside it → the container crash-loops on startup.

- The app is on `http://localhost:8000`.
- On first start the DB is seeded in `./data/hotdesk.db` and the default
  admin credentials are printed to the logs: `docker compose logs`.
- `hotdesk.db` persists in `./data/`: `docker compose down` does not delete
  it. To wipe the database for real, delete the file by hand.
- To update with code changes: `docker compose up --build -d` (the DB in
  `./data/` is preserved; the startup migration in
  [DATABASE.md](DATABASE.md) §5 upgrades the schema in place).
- Backup: `docker compose stop && cp data/hotdesk.db /backup/ && docker
  compose start` (or `sqlite3 data/hotdesk.db ".backup …"` while running).

### 6.5 Migrating from an old deployment (named volume `hotdesk-data`)

Older versions mounted a **named volume** `hotdesk-data`. The switch to a
bind mount does **not** migrate the database automatically: if you start
directly, the app creates an empty DB in `./data/` and the old one (with
employees, bookings, and the admin password) is orphaned in the volume. To
migrate:

```bash
docker compose down
# copy the DB out of the old named volume into ./data/
docker run --rm -v its-app-hotdesk_hotdesk-data:/vol -v "$PWD/data":/out alpine \
  cp /vol/hotdesk.db /out/hotdesk.db
chown 10001:10001 data/hotdesk.db
docker compose up --build -d
```

(The volume name is prefixed with the project directory name — confirm with
`docker volume ls`.)

### 6.6 Exposing the port

By default compose publishes `8000:8000` on all interfaces. Once a reverse
proxy is in front, bind to loopback only so port 8000 isn't reachable from
the network:

```yaml
    ports:
      - "127.0.0.1:8000:8000"
```

---

## 7. Non-Docker deployment (systemd, for reference)

If you'd rather not use Docker, the app is a plain uvicorn process:

```ini
# /etc/systemd/system/hotdesk.service
[Unit]
Description=Hotdesk ITS
After=network.target

[Service]
WorkingDirectory=/opt/hotdesk
EnvironmentFile=/etc/hotdesk/hotdesk.env     # SECRET_KEY, SMTP_*, BOOKING_LOCK_HOUR
Environment=TZ=Europe/Madrid
ExecStart=/opt/hotdesk/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
User=hotdesk

[Install]
WantedBy=multi-user.target
```

With this layout the database defaults to `/opt/hotdesk/hotdesk.db` (set
`HOTDESK_DB` in the env file to move it). Everything else — seeding,
migration, the day lock — behaves identically.

---

## 8. Operations

| Task | How |
| --- | --- |
| Logs (Docker) | `docker compose logs -f` |
| Logs (systemd) | `journalctl -u hotdesk -f` |
| Default admin password | Printed **once** on first seed, to the console/logs. If lost, reset the `adminuser` row directly (re-hash with `app.security.hash_password`) — there is no self-service admin reset. |
| Change the admin password | `/admin/password` (current + new + confirm, min 8 chars). |
| Rotate `SECRET_KEY` | Set the new value and restart. **Logs out every employee and admin** (cookies are signed with it). |
| Move the database | Stop, copy the `.db` to the new location, point `HOTDESK_DB` / the bind mount there, start. |
| Wipe and reseed | Stop, delete the `.db` file, start. The seed (desks, teams, admin) runs again. |
| Add a desk / change positions | Edit `db.py::seed_desks` and start from an empty DB (no admin UI for the desk inventory yet — known missing feature). |

### Timezone checklist

- Container: `TZ=Europe/Madrid` is set in both the Dockerfile and the
  compose file.
- `BOOKING_LOCK_HOUR` is interpreted in that timezone.
- `created_at` timestamps are UTC (arrival order only — never compared to
  wall-clock time).

---

## 9. Security checklist (before going live)

- [ ] `SECRET_KEY` set to a long random value (not the dev default).
- [ ] Default admin password (`admin123`) changed on first real start.
- [ ] Served over HTTPS via a reverse proxy ([HTTPS.md](HTTPS.md)) —
      session cookies only get `Secure` behind a proxy that forwards
      `X-Forwarded-Proto`.
- [ ] Port 8000 not exposed to the network (`127.0.0.1:8000:8000` in
      compose, or `--host 127.0.0.1` under systemd).
- [ ] `data/` owned by uid 10001 (Docker) / the service user (systemd).
- [ ] `SMTP_PASSWORD` and `SECRET_KEY` live in the environment, never in the
      repo (`.env` is in `.gitignore`/`.dockerignore`).
- [ ] A backup routine for the single `.db` file exists (§6.4).

---

## 10. Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| Container crash-loops on first start | `data/` missing or owned by root → `mkdir -p data && chown 10001:10001 data` (§6.4). |
| `docker compose up` fails with "define SECRET_KEY in .env" | `SECRET_KEY` empty in `.env` — the compose file makes it mandatory on purpose. |
| Magic links arrive as `http://` or cookies lack `Secure` | The proxy isn't forwarding `X-Forwarded-Proto` (see [HTTPS.md](HTTPS.md) §1). |
| "database is locked" errors under load | DB on a network filesystem (NFS/Lustre) — move it to local disk ([DATABASE.md](DATABASE.md) §6). |
| 503 "Could not record the booking, please retry" | Sustained write contention on the same day; the user should simply retry. Persistent 503s indicate a deeper problem (e.g. two processes on one DB file). |
| Employee stuck "waitlisted" with no desk | Normal if the office is genuinely full; they're promoted automatically when a desk frees up. Verify with the admin reassign view. |
| Forgot the default admin password before changing it | Reset the `adminuser` row directly in SQLite (re-hash with `app.security.hash_password`). |
