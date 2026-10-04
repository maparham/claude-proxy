import logging

import httpx
import pytest

from claude_proxy import mail, turnstile
from claude_proxy.config import Config, ConfigError, EmailConfig, TicketsConfig


def load(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return Config.load(str(p))


EMAIL = '[email]\nsmtp_host = "smtp.example.com"\nfrom = "Gateway <gw@example.com>"\nadmin_to = "admin@example.com"\n'


def test_email_section_loads_with_defaults(tmp_path):
    e = load(tmp_path, EMAIL).email
    assert (e.smtp_host, e.from_, e.admin_to, e.smtp_port, e.smtp_user) == (
        "smtp.example.com", "Gateway <gw@example.com>", "admin@example.com", 587, "")


def test_no_email_section_means_no_mail(tmp_path):
    assert load(tmp_path, "[tickets]\n").email is None and Config().email is None


@pytest.mark.parametrize("body,needle", [
    ('[email]\nsmtp_host = "h"\nfrom = "f@x.y"\n', "admin_to"),
    ('[email]\nfrom = "f@x.y"\nadmin_to = "a@x.y"\n', "smtp_host"),
    ('[email]\nsmtp_host = "h"\nadmin_to = "a@x.y"\n', "from"),
    (EMAIL + 'bogus = 1\n', "bogus"),
    (EMAIL + 'smtp_port = "587"\n', "smtp_port"),
    (EMAIL + 'smtp_port = 0\n', "smtp_port"),
    ('[email]\nsmtp_host = 5\nfrom = "f@x.y"\nadmin_to = "a@x.y"\n', "smtp_host"),
])
def test_bad_email_config_is_refused(tmp_path, body, needle):
    with pytest.raises(ConfigError) as e:
        load(tmp_path, body)
    assert needle in str(e.value)


def test_smtp_password_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    e = load(tmp_path, EMAIL + 'smtp_user = "gw@example.com"\nsmtp_port = 465\n').email
    assert e.smtp_user == "gw@example.com" and e.smtp_port == 465 and e.password() is None
    monkeypatch.setenv("SMTP_PASSWORD", "s3cret")
    assert e.password() == "s3cret"


def test_turnstile_needs_the_site_key_and_the_secret(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("TURNSTILE_SECRET", raising=False)
    assert TicketsConfig().turnstile_site_key == "" and TicketsConfig().turnstile_on() is False
    with caplog.at_level(logging.WARNING, logger="claude_proxy"):
        cfg = load(tmp_path, '[tickets]\nturnstile_site_key = "0xabc"\n')
    assert cfg.tickets.turnstile_site_key == "0xabc" and cfg.tickets.turnstile_on() is False
    assert "TURNSTILE_SECRET" in caplog.text
    monkeypatch.setenv("TURNSTILE_SECRET", "sec")
    assert cfg.tickets.turnstile_secret() == "sec" and cfg.tickets.turnstile_on() is True


def test_no_warning_without_a_site_key(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("TURNSTILE_SECRET", raising=False)
    with caplog.at_level(logging.WARNING, logger="claude_proxy"):
        load(tmp_path, "[tickets]\n")
    assert "TURNSTILE_SECRET" not in caplog.text


def test_an_smtp_user_without_a_password_warns_at_load(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    body = '[email]\nsmtp_host = "h"\nfrom = "f@x.com"\nadmin_to = "a@x.com"\n'
    with caplog.at_level(logging.WARNING, logger="claude_proxy"):
        load(tmp_path, body)
    assert "SMTP_PASSWORD" not in caplog.text                       # no user: no login, no password needed
    with caplog.at_level(logging.WARNING, logger="claude_proxy"):
        load(tmp_path, body + 'smtp_user = "u"\n')
    assert "SMTP_PASSWORD" in caplog.text


# ---------- mail.send ----------

class FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.calls = host, port, timeout, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.calls.append(("quit",))

    def starttls(self, **kw):
        self.calls.append(("starttls",))

    def login(self, user, pw):
        self.calls.append(("login", user, pw))

    def send_message(self, msg, to_addrs=None):
        self.calls.append(("send", msg))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(mail.smtplib, "SMTP", type("SMTP", (FakeSMTP,), {}))
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", type("SMTP_SSL", (FakeSMTP,), {}))
    return FakeSMTP.instances


def test_send_uses_starttls_and_no_login_without_a_user(smtp):
    mail.send(EmailConfig("smtp.example.com", "Gateway <gw@example.com>", "admin@example.com"), "bob@example.com", "Hi", "Body\n")
    (s,) = smtp
    assert type(s).__name__ == "SMTP" and (s.host, s.port, s.timeout) == ("smtp.example.com", 587, 20)
    assert [c[0] for c in s.calls] == ["starttls", "send", "quit"]
    msg = s.calls[1][1]
    assert (msg["To"], msg["From"], msg["Subject"]) == ("bob@example.com", "Gateway <gw@example.com>", "Hi")
    assert msg.get_content() == "Body\n"


def test_send_logs_in_with_the_user_and_env_password(smtp, monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    mail.send(EmailConfig("h", "f@x.y", "a@x.y", smtp_user="gw"), "b@x.y", "S", "B")
    assert ("login", "gw", "pw") in smtp[0].calls


def test_port_465_is_implicit_tls(smtp):
    mail.send(EmailConfig("h", "f@x.y", "a@x.y", smtp_port=465), "b@x.y", "S", "B")
    (s,) = smtp
    assert type(s).__name__ == "SMTP_SSL" and s.port == 465 and s.timeout == 20
    assert "starttls" not in [c[0] for c in s.calls]


def test_send_raises_on_failure(monkeypatch):
    class Down(FakeSMTP):
        def send_message(self, msg, to_addrs=None):
            raise OSError("connection reset")
    monkeypatch.setattr(mail.smtplib, "SMTP", Down)
    with pytest.raises(OSError):
        mail.send(EmailConfig("h", "f@x.y", "a@x.y"), "b@x.y", "S", "B")


# ---------- turnstile.verify ----------

def fake_cloudflare(monkeypatch, handler):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)
    real = httpx.AsyncClient
    monkeypatch.setattr(turnstile, "_client", lambda: real(transport=httpx.MockTransport(wrapped), timeout=10))
    return seen


@pytest.mark.parametrize("success", [True, False])
async def test_verify_reports_cloudflares_answer(monkeypatch, success):
    seen = fake_cloudflare(monkeypatch, lambda r: httpx.Response(200, json={"success": success}))
    assert await turnstile.verify("sec", "tok", "1.2.3.4") is success
    (req,) = seen
    assert str(req.url) == turnstile.SITEVERIFY
    form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
    assert form == {"secret": "sec", "response": "tok", "remoteip": "1.2.3.4"}


async def test_verify_without_an_ip_omits_it(monkeypatch):
    seen = fake_cloudflare(monkeypatch, lambda r: httpx.Response(200, json={"success": True}))
    assert await turnstile.verify("sec", "tok", None) is True
    assert "remoteip" not in seen[0].content.decode()


async def test_unreachable_cloudflare_raises(monkeypatch):
    def down(request):
        raise httpx.ConnectError("no route")
    fake_cloudflare(monkeypatch, down)
    with pytest.raises(turnstile.TurnstileUnavailable):
        await turnstile.verify("sec", "tok", None)


async def test_cloudflare_5xx_or_garbage_raises(monkeypatch):
    fake_cloudflare(monkeypatch, lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(turnstile.TurnstileUnavailable):
        await turnstile.verify("sec", "tok", None)
    fake_cloudflare(monkeypatch, lambda r: httpx.Response(200, text="not json"))
    with pytest.raises(turnstile.TurnstileUnavailable):
        await turnstile.verify("sec", "tok", None)


async def test_a_non_true_success_is_a_failure(monkeypatch):
    fake_cloudflare(monkeypatch, lambda r: httpx.Response(200, json={"success": "yes"}))
    assert await turnstile.verify("sec", "tok", None) is False
