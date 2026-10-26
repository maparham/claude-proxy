"""Plain-text email over SMTP (order requests design, section 5). One function, so other alerts can reuse it.
A right-to-left body (Persian) also gets an HTML copy set right to left: mail clients lay plain text out left to right.

Blocking: call it with `asyncio.to_thread`, never on the event loop.
"""
from __future__ import annotations

import html
import smtplib
import ssl
import unicodedata
from email.message import EmailMessage

from .config import EmailConfig

TIMEOUT_S = 20


def rtl(text: str) -> bool:
    """Whether the text's first strong character is right to left, as a browser's dir="auto" would judge it."""
    for ch in text:
        d = unicodedata.bidirectional(ch)
        if d in ("R", "AL"):
            return True
        if d == "L":
            return False
    return False


def message(email: EmailConfig, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = email.from_
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if rtl(body):
        msg.add_alternative(f'<div dir="rtl" style="text-align:right;white-space:pre-wrap">{html.escape(body)}</div>\n',
                            subtype="html")
    return msg


def send(email: EmailConfig, to: str, subject: str, body: str) -> None:
    """Send one message. Raises on any failure (connection, TLS, auth, refusal); the caller records it."""
    msg = message(email, to, subject, body)
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
        s.send_message(msg, to_addrs=[to])   # only the validated address, whatever the header parses to
