"""Plain-text email over SMTP (order requests design, section 5). One function, so other alerts can reuse it.
A right-to-left body (Persian) also gets an HTML copy set right to left: mail clients lay plain text out left to right.

Blocking: call it with `asyncio.to_thread`, never on the event loop.
"""
from __future__ import annotations

import html
import logging
import smtplib
import sqlite3
import ssl
import unicodedata
from email.message import EmailMessage

from . import db
from .config import EmailConfig

TIMEOUT_S = 20
SERVER_KEY = "mail_server"   # settings row: "alt" while the admin has [email.alt] go first

logger = logging.getLogger(__name__)


def alt_first(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SERVER_KEY,)).fetchone()
    return row is not None and row[0] == "alt"


def set_alt_first(conn: sqlite3.Connection, actor: int, value: bool) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (SERVER_KEY, "alt" if value else "main"))
    db.audit(conn, actor, "mail_server", "alt" if value else "main")


def servers(conn: sqlite3.Connection, email: EmailConfig) -> list[EmailConfig]:
    """The SMTP servers to try, in order: the admin's pick on the Orders tab first, the other as the fallback.
    Read on the loop (the shared connection), then handed to `send` in its thread."""
    if email.alt is None:
        return [email]
    return [email.alt, email] if alt_first(conn) else [email, email.alt]


def rtl(text: str) -> bool:
    """Whether the text's first strong character is right to left, as a browser's dir="auto" would judge it."""
    for ch in text:
        d = unicodedata.bidirectional(ch)
        if d in ("R", "AL"):
            return True
        if d == "L":
            return False
    return False


def message(email: EmailConfig, to: str, subject: str, body: str, html_body: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = email.from_fa if email.from_fa and rtl(body) else email.from_
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    elif rtl(body):
        msg.add_alternative(f'<div dir="rtl" style="text-align:right;white-space:pre-wrap">{html.escape(body)}</div>\n',
                            subtype="html")
    return msg


def send(email: EmailConfig | list[EmailConfig], to: str, subject: str, body: str, html_body: str | None = None) -> None:
    """Send one message, with `html_body` as its HTML copy if given. Given several servers (`servers`), each is tried
    in turn until one takes it. Raises the last failure (connection, TLS, auth, refusal); the caller records it."""
    order = email if isinstance(email, list) else [email]
    for i, server in enumerate(order):
        try:
            _send(server, to, subject, body, html_body)
        except Exception as e:
            if i == len(order) - 1:
                raise
            logger.warning("mail to %s through %s failed (%s); trying %s", to, server.name(), e, order[i + 1].name())
        else:
            if i:
                logger.info("mail to %s sent through %s", to, server.name())
            return


def _send(email: EmailConfig, to: str, subject: str, body: str, html_body: str | None) -> None:
    msg = message(email, to, subject, body, html_body)
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
