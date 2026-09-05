# HTTPS and Domain Names

How to serve the app at `https://your-domain` instead of
`http://server-ip:8000`. This complements [DEPLOYMENT.md](DEPLOYMENT.md);
the app-side prerequisites (env vars, port binding) are documented there.

---

## 1. How it works (1-minute version)

The container **keeps speaking plain HTTP on port 8000**. HTTPS is added by
a **reverse proxy** (Caddy or nginx) in front of it:

```
  user ──HTTPS (443)──> reverse proxy ──HTTP (8000)──> hotdesk container
                            │
                            └── the certificate lives here, not in the app
```

- The **certificate** is managed by the proxy, not the app. The app changes
  nothing.
- The app already trusts the proxy's headers: the Dockerfile's `CMD` runs
  uvicorn with `--proxy-headers --forwarded-allow-ips "*"`, so uvicorn
  honors `X-Forwarded-Proto` and `X-Forwarded-For`. Consequences, with zero
  code changes:
  - Magic links are generated from `request.base_url`, so they come out as
    `https://your-domain/…`.
  - Session cookies get the `Secure` flag automatically when the request
    arrived over HTTPS (`secure=request.url.scheme == "https"`).
  - If the proxy does **not** forward `X-Forwarded-Proto`, both of the above
    silently break (links come out `http://`, cookies lack `Secure`).

**Requirements for the Let's Encrypt options (A and B):**

1. A domain name (e.g. `hotdesk.irbbarcelona.org`) with a **DNS A record**
   pointing at the server's public IP.
2. Ports **80 and 443** open to the server (Let's Encrypt uses them to
   validate the domain).
3. The domain must be **reachable from the Internet**. If the app lives only
   on the IRB intranet and is not visible from outside, Let's Encrypt cannot
   work → go straight to **option C**.

> Once the proxy is up, port 8000 should not be reachable from the network
> (only from the server itself). In `docker-compose.yml`, change
> `"8000:8000"` to `"127.0.0.1:8000:8000"`.

---

## 2. Option A — Caddy (recommended: zero maintenance)

[Caddy](https://caddyserver.com/) requests, installs, and **renews** the
Let's Encrypt certificate on its own. It is the option with the least
configuration.

### 2.1 Install

```bash
# Debian/Ubuntu
sudo apt install caddy
```

### 2.2 Configure

Edit `/etc/caddy/Caddyfile`:

```
hotdesk.irbbarcelona.org {
    reverse_proxy 127.0.0.1:8000
}
```

That's all. Reload:

```bash
sudo systemctl reload caddy
```

### 2.3 What Caddy does by itself

- Requests the certificate from Let's Encrypt the first time anyone visits
  the domain (HTTP validation on port 80).
- Stores it in `/var/lib/caddy` and renews it automatically (~every 60 days).
- Redirects `http://` to `https://` automatically.
- Forwards `X-Forwarded-Proto`/`X-Forwarded-For` for you (required by the
  app — see §1).

### 2.4 Verify

```bash
curl -I https://hotdesk.irbbarcelona.org
# HTTP/2 200, header "server: Caddy"
```

Open the browser, log in, and check that the magic link in the email starts
with `https://hotdesk.irbbarcelona.org/…`.

---

## 3. Option B — nginx + certbot

More manual than Caddy, but the most common setup on Linux servers.

### 3.1 Install

```bash
sudo apt install nginx certbot python3-certbot-nginx
```

### 3.2 Configure nginx

Create `/etc/nginx/sites-available/hotdesk`:

```nginx
server {
    listen 80;
    server_name hotdesk.irbbarcelona.org;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/hotdesk /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

> The `proxy_set_header X-Forwarded-*` lines are **required**: they tell the
> app "the user arrived over HTTPS". Magic-link URLs and the `Secure`
> cookie flag depend on them (see §1).

### 3.3 Request the certificate

```bash
sudo certbot --nginx -d hotdesk.irbbarcelona.org
```

Certbot edits the nginx config, adds the `listen 443 ssl` block with the
`/etc/letsencrypt/` certificates, and the http→https redirect. Answer the
prompts (contact email, terms) and you're done.

### 3.4 Automatic renewal

Certbot installs a timer by itself; verify with:

```bash
sudo certbot renew --dry-run
```

---

## 4. Option C — No public domain (intranet / self-signed)

If the app isn't visible from the Internet, Let's Encrypt can't validate
the domain. Two ways forward:

### 4.1 The institution's CA (best, if it exists)

If IRB has an internal CA that staff machines already trust, request a
certificate for the internal FQDN (e.g. `hotdesk.irb.sc.irbbarcelona.org`)
and configure it in the proxy exactly as in options A/B (Caddy: `tls
/path/cert.pem /path/key.pem` inside the site block; nginx:
`ssl_certificate` / `ssl_certificate_key`).

### 4.2 Self-signed certificate (works, with a browser warning)

```bash
openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
  -keyout hotdesk.key -out hotdesk.crt \
  -subj "/CN=hotdesk.irb.sc.irbbarcelona.org" \
  -addext "subjectAltName=DNS:hotdesk.irb.sc.irbbarcelona.org"
```

And in nginx, inside the `server` block with `listen 443 ssl`:

```nginx
ssl_certificate     /etc/nginx/ssl/hotdesk.crt;
ssl_certificate_key /etc/nginx/ssl/hotdesk.key;
```

The browser shows a security warning on first visit (normal for
self-signed); accept with "proceed anyway". To remove the warning you must
distribute the certificate as a trusted root on the machines (which is
exactly what an internal CA does).

> Alternative without a proxy: some environments put the self-signed
> certificate directly in the container. **Not recommended**: the app is not
> designed to terminate TLS itself, and you lose the proxy/application
> separation.

---

## 5. Final checklist

- [ ] DNS: the domain points at the server.
- [ ] Ports 80/443 open (options A/B).
- [ ] Proxy configured and reloaded; `X-Forwarded-Proto` is forwarded.
- [ ] `curl -I https://the-domain` → 200.
- [ ] Real login: the magic link in the email starts with
      `https://the-domain`.
- [ ] In the browser, the padlock is present and the `session` cookie has
      `Secure` (F12 → Application → Cookies).
- [ ] `SECRET_KEY` defined in `.env` (mandatory — see
      [DEPLOYMENT.md](DEPLOYMENT.md) §6.3).
- [ ] (Optional) compose: `"127.0.0.1:8000:8000"` so port 8000 isn't
      exposed.

---

## 6. Where the database lives

The database path is configurable via the `HOTDESK_DB` environment variable
(`app/db.py`):

| Environment | Default value | Where the file ends up |
| --- | --- | --- |
| Without Docker (`uv run uvicorn …`) | `hotdesk.db` in the **project root** | In the repo checkout itself (in `.gitignore`) |
| Docker (`docker compose up`) | `/data/hotdesk.db` **inside the container** | In the `data/` directory **next to `docker-compose.yml`** (bind mount `./data:/data`) |

So with Docker the database lives at `<repo>/data/hotdesk.db` on the host:
directly visible, and the backup is a `cp` of that file (with the container
stopped, to avoid copying SQLite mid-write).

If you prefer another location, change the bind mount in `docker-compose.yml`
to an absolute path:

```yaml
    volumes:
      - /absolute/path/on/host/hotdesk:/data
```

(Nothing else changes: `HOTDESK_DB=/data/hotdesk.db` remains the
container-internal path.) To move an existing database: stop the container,
copy the `.db` to the new location, start again.

> If the directory lives on a network filesystem (NFS/Lustre), SQLite can
> have locking problems under concurrency. For this app (few users, one
> writer process) it usually works; if you see "database is locked" errors
> under load, move the DB to local disk.
