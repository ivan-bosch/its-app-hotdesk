"""Password hashing helpers — standalone module (no DB/model imports) so both
db.py (seeding the default admin) and admin_auth.py (verifying logins) can use
it without creating an import cycle. Also owns the SECRET_KEY used to sign
both employee and admin session cookies, so the two auth modules can't drift
onto different keys."""

import hashlib
import os
import secrets

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"

PBKDF2_ITERATIONS = 260_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return secrets.compare_digest(candidate, password_hash)
