# Guía de instalación y puesta en marcha — Hotdesk ITS

Esta guía complementa el README (que tiene el detalle técnico del algoritmo y
la estructura de código) con los pasos concretos para instalar, arrancar y
configurar la app desde cero, incluyendo el envío real de email por Gmail.

---

## 1. Requisitos previos

- Python 3.11 (fijado en `.python-version`)
- [`uv`](https://docs.astral.sh/uv/) para gestionar dependencias y el entorno
  virtual (ya hay un `.venv/` creado en este repo, y un `uv.lock` con las
  versiones exactas)

Si no tienes `uv` instalado:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 2. Instalar dependencias

Desde la raíz del proyecto:

```bash
uv sync
```

Esto crea/actualiza `.venv/` con exactamente lo que pide `uv.lock`
(FastAPI, SQLModel, uvicorn, itsdangerous, python-multipart, jinja2).

## 3. Arrancar la app (modo local, sin email real)

```bash
uv run uvicorn app.main:app --reload --port 8000
```

Abre `http://localhost:8000`. La primera vez que arranca:

- Crea `hotdesk.db` (SQLite, en `.gitignore`) y siembra los puestos del plano
  real, los equipos por defecto (Data, SciCom, AI, Security, Help Desk) y una
  cuenta de administrador.
- Imprime en la consola del servidor el usuario/contraseña del admin:

  ```
  --- ADMIN ACCOUNT CREATED ---
  username: admin
  password: admin123
  Change this password from /admin/password after logging in.
  ---
  ```

En este modo (sin SMTP configurado), el login de empleados por magic link
**no envía emails de verdad** — el enlace se imprime en la consola del
servidor, justo igual que la contraseña de admin. Es el comportamiento por
defecto, pensado para desarrollar sin depender de ningún servidor de correo.

### Primeros pasos recomendados

1. Entra en `http://localhost:8000/admin/login` con `admin` / `admin123`.
2. Ve a `/admin/password` y cambia la contraseña inmediatamente.
3. Revisa `/admin/teams` y `/admin/employees` para dar de alta a la gente real
   (o deja que cada persona se registre sola con su email vía `/login` — se
   crea el `Employee` automáticamente al verificar el primer magic link).

## 4. Configurar el email real (Gmail / cuenta de IRB Barcelona)

Ahora mismo el email **no está configurado** — la variable `SMTP_HOST` no
está definida en tu entorno, así que `app/auth.py::send_email` usa el
fallback de consola. Para activar el envío real por Gmail:

### 4.1 Crear una contraseña de aplicación en la cuenta de Gmail

Gmail no permite usar la contraseña normal de la cuenta para SMTP si tienes
verificación en dos pasos activada (obligatoria en cuentas de Google
Workspace como la de IRB). Pasos:

1. Entra en la cuenta de Google que enviará los correos (p. ej.
   `hotdesk@irbbarcelona.org` o la cuenta que se decida usar).
2. Ve a **myaccount.google.com/security**.
3. Activa **Verificación en dos pasos** si no lo está ya.
4. Ve a **Contraseñas de aplicaciones** (`myaccount.google.com/apppasswords`).
5. Crea una nueva contraseña de aplicación (nombre sugerido: "Hotdesk SMTP").
   Google te da una contraseña de 16 caracteres — cópiala, solo se muestra
   una vez.

> Si la cuenta es de **Google Workspace** (como probablemente sea la de IRB
> Barcelona), puede que un administrador del dominio tenga que habilitar
> "Acceso de apps menos seguras" o las contraseñas de aplicación a nivel de
> organización antes de que el paso anterior esté disponible. Si no aparece
> la opción, contacta con el administrador de Google Workspace del instituto.

### 4.2 Definir las variables de entorno

Antes de arrancar la app, exporta (o añade a tu gestor de procesos /
`systemd` / Docker) estas variables:

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="hotdesk@irbbarcelona.org"       # la cuenta de Gmail remitente
export SMTP_PASSWORD="xxxxxxxxxxxxxxxx"            # la contraseña de aplicación de 16 caracteres, sin espacios
export SMTP_FROM="hotdesk@irbbarcelona.org"        # opcional; por defecto usa SMTP_USER
export SMTP_USE_TLS="true"                          # opcional; Gmail requiere TLS, ya es el valor por defecto
```

También conviene fijar un secreto real para las cookies de sesión (por
defecto usa uno de desarrollo inseguro):

```bash
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

Guarda estas variables donde vayas a desplegar (no las subas al repo). Una
forma cómoda en local es un archivo `.env` cargado con `direnv` o
`python-dotenv`, aunque ahora mismo la app no carga `.env` automáticamente —
solo lee variables ya presentes en el entorno del proceso.

### 4.3 Arrancar con esas variables activas

```bash
uv run uvicorn app.main:app --port 8000
```

Con `SMTP_HOST` definido, `send_email` ya no imprime nada por consola: envía
el email de verdad vía `smtp.gmail.com:587` con STARTTLS + login. Prueba el
flujo de login (`/login` con tu email) y confirma que te llega el correo con
el enlace mágico.

### 4.4 Solución de problemas SMTP

- **`(535, '5.7.8 Username and Password not accepted')`**: la contraseña de
  aplicación es incorrecta, caducó, o la verificación en dos pasos no está
  activa en la cuenta remitente.
- **Timeout / conexión rechazada**: revisa que el `SMTP_PORT` (587) no esté
  bloqueado por el firewall de la red desde donde corre la app.
- **El correo se envía pero no llega**: revisa la carpeta de spam; Gmail
  aplica límites de envío por cuenta (500 emails/día en cuentas normales,
  2000 en Workspace) — de sobra para el uso interno de esta app.
- Si necesitas depurar sin arriesgar el envío real, deja `SMTP_HOST` sin
  definir temporalmente: vuelve al modo consola.

## 5. El bloqueo diario de elecciones (`BOOKING_LOCK_HOUR`)

La asignacinnn de escritorios es **instantnea**: marcar un d como
"presencial" asigna (o reasigna) escritorio en el momento, recalculando el
d entero con el algoritmo de
[`ALGORITHM.md`](ALGORITHM.md). No hay ningn job programado ni estado
"pendiente".

Lo nico configurable es la **hora de cierre**: a partir de las
`BOOKING_LOCK_HOUR`:00 de cada d (hora local del servidor), ese d queda
bloqueado y los empleados ya no pueden cambiar su eleccinnn (ni presencial,
ni remoto, ni vacaciones). Los ds futuros siguen abiertos, y el admin
siempre puede reasignar manualmente desde `/admin/reassign`, incluso despus
del cierre.

- Valor por defecto: `8` (las 08:00). Solo admite horas enteras (0-23):
  ```bash
  export BOOKING_LOCK_HOUR="9"   # los ds se cierran a las 09:00 hora del servidor
  ```
- **Importante sobre zona horaria**: el cierre usa la hora local del
  servidor (`datetime.now()`), no UTC. Si el servidor donde se despliega la
  app corre en una zona horaria distinta a la de la oficina de Barcelona,
  hay que configurar bien la zona horaria del sistema operativo del servidor
  (o ajustar `BOOKING_LOCK_HOUR` para compensar la diferencia).

## 6. Desplegar en un servidor (resumen)

El README señala explícitamente que **no está decidido dónde se aloja** la
app. Cuando se decida, como mínimo habrá que:

1. Definir `SECRET_KEY` y las variables `SMTP_*` como variables de entorno
   del proceso/contenedor (no hardcodeadas).
2. Servir con un proceso persistente, p. ej.:
   ```bash
   uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
   detrás de un reverse proxy (nginx/Caddy) con HTTPS.
3. Asegurarse de que `hotdesk.db` vive en un volumen persistente (no se
   pierde en cada despliegue) — es SQLite, un único archivo.
4. Cambiar la contraseña del admin por defecto (`admin`/`admin123`) en el
   primer arranque en el entorno real, igual que en local.
5. Revisar la zona horaria del servidor/contenedor (`TZ`) para que
   `BOOKING_LOCK_HOUR` cierre los días a la hora local de la oficina, no a
   otra hora por estar el servidor en UTC u otra zona (ver sección 5
   arriba).

## 7. Despliegue con Docker

El repo incluye `Dockerfile`, `docker-compose.yml` y `.env.example`. La app
corre en un contenedor `python:3.11-slim` (dependencias instaladas con `uv`
desde `uv.lock`), como usuario no-root, con `TZ=Europe/Madrid`. La base de
datos SQLite vive fuera del contenedor, en un volumen: dentro se crea en
`/data/hotdesk.db` (variable `HOTDESK_DB`), que el compose monta como
volumen `hotdesk-data`.

### 7.1 Preparar las variables

```bash
cp .env.example .env
```

Y rellenar en `.env`:

- `SECRET_KEY` (obligatorio):
  `python3 -c 'import secrets; print(secrets.token_hex(32))'`
- Las `SMTP_*` solo si quieres email real (sección 4). Sin `SMTP_HOST`, los
  magic links se imprimen por consola en lugar de enviarse.

### 7.2 Arrancar

```bash
docker compose up --build -d
```

- La app queda en `http://localhost:8000`.
- En el primer arranque se siembra la DB en el volumen y se imprime la
  cuenta admin por defecto en los logs: `docker compose logs`.
- `hotdesk.db` persiste en el volumen: `docker compose down` no lo borra
  (`docker compose down -v` sí lo borra).
- Para actualizar con cambios de código: `docker compose up --build -d`
  (el volumen con la DB se conserva).

### 7.3 Notas

- El contenedor usa `TZ=Europe/Madrid`, así que `BOOKING_LOCK_HOUR` cierra
  los días a la hora de la oficina sin compensaciones (sección 5).
- Cambiar la contraseña del admin por defecto en el primer arranque real
  (sección 6, punto 4).
- HTTPS/reverse proxy (Caddy/nginx) es una capa aparte, por delante del
  puerto 8000.

---

**Resumen rápido de lo que falta hacer ahora mismo para tener email real:**
crear la contraseña de aplicación de Gmail (paso 4.1) y exportar las 3
variables `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` antes de arrancar la app
(paso 4.2). Sin eso, todo sigue funcionando igual mostrando los enlaces por
consola.
