"""Plain-text email over SMTP (order requests design, section 5). One function, so other alerts can reuse it.

Blocking: call it with `asyncio.to_thread`, never on the event loop.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .config import EmailConfig

TIMEOUT_S = 20


def send(email: EmailConfig, to: str, subject: str, body: str) -> None:
    """Send one message. Raises on any failure (connection, TLS, auth, refusal); the caller records it."""
    msg = EmailMessage()
    msg["From"] = email.from_
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if email.smtp_port == 465:   # implicit TLS
        server = smtplib.SMTP_SSL(email.smtp_host, email.smtp_port, timeout=TIMEOUT_S, context=ctx)
    else:
        server = smtplib.SMTP(email.smtp_host, email.smtp_port, timeout=TIMEOUT_S)
    with server as s:
        if email.smtp_port != 465:
            s.starttls(context=ctx)
        if email.smtp_user:
            s.login(email.smtp_user, email.password() or "")
        s.send_message(msg)
