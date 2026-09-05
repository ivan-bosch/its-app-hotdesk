"""Email delivery, shared by auth (magic links) and assignment (waitlist
notifications).

Sends via SMTP when SMTP_HOST is configured (works with Gmail, Office365, or
any institutional mail relay — see docs/DEPLOYMENT.md for the env vars).
Falls
back to printing to the server console when SMTP_HOST is unset, so everything
still works end-to-end in local development without any mail server.

`raise_on_error` controls what happens when SMTP is configured but the send
fails: True (default) raises HTTPException 500 — right for magic links, where
the user is staring at a "check your email" page that would otherwise lie.
Notification callers pass False: a waitlist email is best-effort and must
never break the booking flow that triggered it.
"""

import os
import smtplib
from email.message import EmailMessage

from fastapi import HTTPException

SMTP_HOST = os.environ.get("SMTP_HOST")
try:
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
except ValueError:
    SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "hotdesk@localhost")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() not in ("0", "false", "no")


def send_email(to: str, subject: str, body: str, *, raise_on_error: bool = True) -> None:
    if not SMTP_HOST:
        print(f"\n--- EMAIL to {to} ---\n{subject}\n{body}\n---\n")
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM
    message["To"] = to
    message.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls()
            if SMTP_USER and SMTP_PASSWORD:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        if raise_on_error:
            raise HTTPException(status_code=500, detail=f"Could not send email: {exc}") from exc
        print(f"--- EMAIL to {to} FAILED (best-effort notification, ignored): {exc} ---")
