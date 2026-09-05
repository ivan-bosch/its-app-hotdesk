# HTTPS y nombre de dominio — Hotdesk

Guía para servir la app con `https://tu-dominio` en vez de
`http://ip-del-servidor:8000`. Complementa a `SETUP.md` (sección 7).

---

## 1. Cómo funciona (versión 1 minuto)

El contenedor **sigue hablando solo HTTP en el puerto 8000**. Lo que añade
HTTPS es una capa por delante llamada **reverse proxy** (Caddy o nginx):

```
  usuario ──HTTPS (443)──> reverse proxy ──HTTP (8000)──> contenedor hotdesk
                              │
                              └── aquí vive el certificado
```

- El **certificado** lo gestiona el proxy, no la app. La app no cambia nada.
- La app ya confía en los headers del proxy (`--proxy-headers` en el
  `Dockerfile`), así que los magic links salen como
  `https://tu-dominio/...` y las cookies de sesión llevan el flag `Secure`
  automáticamente cuando la petición llega por HTTPS. No hay que tocar código.

**Requisitos para las opciones A y B (Let's Encrypt):**

1. Un nombre de dominio (p. ej. `hotdesk.irbbarcelona.org`) con un registro
   DNS tipo **A** apuntando a la IP pública del servidor.
2. Puertos **80 y 443** abiertos hacia el servidor (Let's Encrypt los usa
   para validar el dominio).
3. El dominio debe ser **alcanzable desde Internet**. Si la app va a vivir
   solo en la intranet del IRB y no es visible desde fuera, Let's Encrypt no
   funciona → ve directamente a la **opción C**.

> Consejo: cuando el proxy esté funcionando, conviene que el puerto 8000 no
> sea accesible desde la red (solo desde el propio servidor). En
> `docker-compose.yml`, cambia `"8000:8000"` por `"127.0.0.1:8000:8000"`.

---

## 2. Opción A — Caddy (recomendada: cero mantenimiento)

[Caddy](https://caddyserver.com/) pide, instala y **renueva solo** el
certificado de Let's Encrypt. Es la opción con menos configuración.

### 2.1 Instalar

```bash
# Debian/Ubuntu
sudo apt install caddy
```

### 2.2 Configurar

Editar `/etc/caddy/Caddyfile`:

```
hotdesk.irbbarcelona.org {
    reverse_proxy 127.0.0.1:8000
}
```

Eso es todo. Recargar:

```bash
sudo systemctl reload caddy
```

### 2.3 Qué hace Caddy solo

- Pide el certificado a Let's Encrypt la primera vez que alguien visita el
  dominio (validación HTTP en el puerto 80).
- Lo guarda en `/var/lib/caddy` y lo renueva automáticamente cada ~60 días.
- Redirige `http://` a `https://` automáticamente.

### 2.4 Verificar

```bash
curl -I https://hotdesk.irbbarcelona.org
# HTTP/2 200, headers "server: Caddy"
```

Abrir el navegador, loguearse, y comprobar que el magic link que llega por
email empieza por `https://hotdesk.irbbarcelona.org/...`.

---

## 3. Opción B — nginx + certbot

Más manual que Caddy, pero es lo más habitual en servidores Linux.

### 3.1 Instalar

```bash
sudo apt install nginx certbot python3-certbot-nginx
```

### 3.2 Configurar nginx

Crear `/etc/nginx/sites-available/hotdesk`:

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

> Los `proxy_set_header X-Forwarded-*` son importantes: son los que le dicen
> a la app "el usuario llegó por HTTPS" (los magic links y las cookies
> `Secure` dependen de ello).

### 3.3 Pedir el certificado

```bash
sudo certbot --nginx -d hotdesk.irbbarcelona.org
```

Certbot edita la config de nginx, añade el `listen 443 ssl` con los
certificados de `/etc/letsencrypt/` y la redirección http→https. Responde a
las preguntas (email de contacto, aceptar términos) y listo.

### 3.4 Renovación automática

Certbot instala un timer solo; verificar con:

```bash
sudo certbot renew --dry-run
```

---

## 4. Opción C — Sin dominio público (intranet / auto-firmado)

Si la app no es visible desde Internet, Let's Encrypt no puede validar el
dominio. Dos caminos:

### 4.1 CA del instituto (lo mejor, si existe)

Si el IRB tiene una CA interna que los navegadores de los equipos ya
confían, pide un certificado para el FQDN interno (p. ej.
`hotdesk.irb.sc.irbbarcelona.org`) y ponlo en el proxy igual que en las
opciones A/B (en Caddy: `tls /ruta/cert.pem /ruta/key.pem` dentro del
bloque del sitio; en nginx: `ssl_certificate` / `ssl_certificate_key`).

### 4.2 Certificado auto-firmado (funciona, con aviso del navegador)

```bash
openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
  -keyout hotdesk.key -out hotdesk.crt \
  -subj "/CN=hotdesk.irb.sc.irbbarcelona.org" \
  -addext "subjectAltName=DNS:hotdesk.irb.sc.irbbarcelona.org"
```

Y en nginx, dentro del bloque `server` con `listen 443 ssl`:

```nginx
ssl_certificate     /etc/nginx/ssl/hotdesk.crt;
ssl_certificate_key /etc/nginx/ssl/hotdesk.key;
```

El navegador mostrará un aviso de seguridad la primera vez (es normal con
auto-firmados); se acepta con "proceed anyway". Para quitar el aviso hay que
distribuir el certificado como raíz de confianza en los equipos (lo que hace
una CA interna).

> Alternativa sin proxy: algunos entornos usan el certificado auto-firmado
> directamente en el contenedor, pero **no** es recomendable: la app no está
> diseñada para terminar TLS ella misma y perderías la separación
> proxy/aplicación.

---

## 5. Checklist final

- [ ] DNS: el dominio apunta al servidor.
- [ ] Puertos 80/443 abiertos (opciones A/B).
- [ ] Proxy configurado y recargado.
- [ ] `curl -I https://el-dominio` → 200.
- [ ] Login real: el magic link del email empieza por `https://el-dominio`.
- [ ] En el navegador, el candado aparece y la cookie `session` tiene
      `Secure` (F12 → Application → Cookies).
- [ ] `SECRET_KEY` definida en `.env` (obligatorio, ver `SETUP.md` §7.1).
- [ ] (Opcional) compose: `"127.0.0.1:8000:8000"` para no exponer el 8000.

---

## 6. Dónde vive la base de datos

La ruta de la DB ya es configurable por variable de entorno: **`HOTDESK_DB`**
(`app/db.py`).

| Entorno | Valor por defecto | Dónde queda el archivo |
| --- | --- | --- |
| Sin Docker (`uv run uvicorn ...`) | `hotdesk.db` en la **raíz del proyecto** | En el propio checkout del repo (está en `.gitignore`) |
| Docker (`docker compose up`) | `/data/hotdesk.db` **dentro del contenedor** | En la carpeta `data/` **junto al `docker-compose.yml`** (bind mount `./data:/data`) |

Es decir, con Docker la DB vive en `<repo>/data/hotdesk.db` en el host:
visible directamente, y el backup es un `cp` de ese fichero (con el
contenedor parado, para no copiar el SQLite a mitad de escritura).

Si prefieres otro sitio, cambia el bind mount en `docker-compose.yml` a una
ruta absoluta:

```yaml
    volumes:
      - /ruta/absoluta/en/el/host/hotdesk:/data
```

(El resto no cambia: `HOTDESK_DB=/data/hotdesk.db` sigue siendo la ruta
dentro del contenedor.) Para mover una DB existente: parar el contenedor,
copiar el `.db` al nuevo sitio, y arrancar de nuevo.

> Nota: si el directorio vive en un filesystem de red (NFS/Lustre), SQLite
> puede dar problemas de locking bajo concurrencia. Para esta app (pocos
> usuarios, un solo proceso escritor) suele funcionar; si ves errores del
> tipo "database is locked" bajo carga, mueve la DB a disco local.
