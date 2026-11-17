"""Free trial (design 2026-11-14): the routes."""
import time

import pytest

from claude_proxy import trials, turnstile
from tests.conftest import asgi_client
from tests.test_tickets_web import env as _tickets_env  # noqa: F401  (the fixture)
from tests.test_web import admin_client, bearer
from tests.tickets_helpers import live
from claude_proxy.web import create_dashboard_app


@pytest.fixture
def env(_tickets_env):
    gw, conn, cfg, ids, keys = _tickets_env
    live(conn, time.time())
    trials.set_settings(conn, cfg, ids["admin"], {"enabled": True})
    return _tickets_env


def client(gw):
    return asgi_client(create_dashboard_app(gw))


async def test_admin_settings_roundtrip(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        r = await c.put("/api/admin/trial", json={"duration_min": 90, "valid_hours": 48})
        assert r.status_code == 200
        s = (await c.get("/api/admin/trial")).json()["settings"]
        assert (s["duration_min"], s["valid_hours"], s["tier"]) == (90, 48, "lite")
        assert (await c.put("/api/admin/trial", json={"duration_min": 0})).status_code == 400
    async with client(gw) as c:
        assert (await c.get("/api/admin/trial", headers=bearer(keys["alice"]))).status_code == 403


async def test_user_claims_and_starts(env):
    gw, conn, cfg, ids, keys = env
    h = bearer(keys["alice"])
    async with client(gw) as c:
        o = (await c.get("/api/me/trial", headers=h)).json()
        assert o["can_claim"] is True and o["claim"] is None and o["duration_min"] == 60
        r = await c.post("/api/me/trial/claim", headers=h, json={})
        assert r.status_code == 200 and r.json()["claim"] is not None and r.json()["can_claim"] is False
        assert (await c.post("/api/me/trial/claim", headers=h, json={})).status_code == 409
        r = await c.post("/api/me/trial/start", headers=h, json={})
        assert r.status_code == 200 and r.json()["ends_at"] - r.json()["starts_at"] == 3600
        assert (await c.post("/api/me/trial/start", headers=h, json={})).status_code == 409
        card = (await c.get("/api/me/tickets", headers=h)).json()
        assert card["current"]["trial"] is True


async def test_claim_checks_turnstile_when_on(env, monkeypatch):
    gw, conn, cfg, ids, keys = env
    cfg.tickets.turnstile_site_key = "0xSITE"
    monkeypatch.setenv("TURNSTILE_SECRET", "sec")
    answer = {"ok": False}

    async def verify(secret, token, ip):
        return answer["ok"]
    monkeypatch.setattr(turnstile, "verify", verify)
    h = bearer(keys["alice"])
    async with client(gw) as c:
        assert (await c.get("/api/me/trial", headers=h)).json()["turnstile_site_key"] == "0xSITE"
        assert (await c.post("/api/me/trial/claim", headers=h, json={"turnstile_token": "t"})).status_code == 400
        assert conn.execute("SELECT COUNT(*) FROM trial_claims").fetchone()[0] == 0
        answer["ok"] = True
        assert (await c.post("/api/me/trial/claim", headers=h, json={"turnstile_token": "t"})).status_code == 200


async def test_admin_grant(env):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        r = await c.post(f"/api/admin/users/{ids['alice']}/trial", json={})
        assert r.status_code == 200
        assert conn.execute("SELECT granted_by FROM trial_claims").fetchone()[0] == ids["admin"]
        assert (await c.post(f"/api/admin/users/{ids['alice']}/trial", json={})).status_code == 409
        assert (await c.post("/api/admin/users/9999/trial", json={})).status_code == 404


async def test_pricing_shows_the_offer_until_the_cap(env):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        assert (await c.get("/api/pricing")).json()["trial"] == {"duration_min": 60, "tier": "lite", "label": "Lite"}
        trials.set_settings(conn, cfg, ids["admin"], {"daily_cap": 0})
        assert (await c.get("/api/pricing")).json()["trial"] is None


# ---------- the dashboard's markup, rendered by app.js in node ----------

UI_STUBS = """
var esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
var tierName = (id, label) => label, tierNameText = (id, label) => label;
var fmtDate = (s) => `D${s}`, tipI = (k) => `[?${k}]`, PCT = "%";
var fmtShare = (v) => `${+v}%`, nfFull = new Intl.NumberFormat("en-US");
"""


def test_trial_card_offers_then_waits(tmp_path):
    from tests.test_tickets_web import _app_fn
    out = _app_fn(tmp_path, ["trialCard"], UI_STUBS + """var out = [
      trialCard({ can_claim: true, claim: null, duration_min: 60, tier: "lite", label: "Lite" }),
      trialCard({ can_claim: false, claim: { expires_at: 99 }, duration_min: 90, tier: "lite", label: "Lite" }),
      trialCard({ can_claim: false, claim: null, duration_min: 60, tier: "lite" }),
      trialCard({ can_claim: true, claim: null, duration_min: 60, tier: "lite", turnstile_site_key: "0x" })];""")
    assert "1 hour on a Lite pass" in out[0] and 'id="trial-claim"' in out[0] and "trial-ts" not in out[0]
    assert "90 minutes" in out[1] and "D99" in out[1] and 'id="trial-start"' in out[1]
    assert out[2] == ""
    assert 'id="trial-ts"' in out[3] and 'id="trial-claim" disabled' in out[3]


def test_trial_settings_card_shows_tier_defaults_empty(tmp_path):
    from tests.test_tickets_web import _app_fn
    view = ('{ settings: { enabled: true, duration_min: 60, valid_hours: 24, tier: "lite", share_pct: 5, share_5h_pct: 5, daily_cap: 5, '
            'follow_pct: 10, follow_days: 7 }, tier_default: { tier: true, share_pct: true, share_5h_pct: false }, today: 2, claimed: 9, '
            'started: 7, lapsed: 1, bought: 3 }')
    out = _app_fn(tmp_path, ["trialSettingsCard", "refSettingsRow", "refWholeInput", "refPctInput"],
                  UI_STUBS + f'var out = trialSettingsCard({view}, {{ lite: {{ label: "Lite" }}, standard: {{ label: "Standard" }} }});')
    assert "2 of 5" in out and "9 claimed" in out
    assert 'name="share_pct" data-init="" value=""' in out          # the tier's own: left empty
    assert 'name="share_5h_pct" data-init="5" value="5"' in out      # set by the admin
    assert '<option value="" selected>' in out                       # tier: the default


# ---------- the claim's email (design 2026-11-17) ----------

import asyncio  # noqa: E402

from claude_proxy import mail, web  # noqa: E402
from claude_proxy.config import EmailConfig  # noqa: E402


@pytest.fixture
def sent(env, monkeypatch):
    """[email] configured, alice with an account email, mail.send faked: the list of (to, subject, body) sent."""
    gw, conn, cfg, ids, keys = env
    cfg.email = EmailConfig("smtp.example.com", "gw@example.com", "admin@example.com")
    cfg.listener.dashboard_url = "https://dash.example.com"
    cfg.listener.public_url = "https://gw.example.com"
    conn.execute("UPDATE users SET email='alice@example.com' WHERE id=?", (ids["alice"],))
    out = []
    monkeypatch.setattr(mail, "send", lambda email, to, subject, body, html=None: out.append((to, subject, body)))
    return out


async def flush():
    await asyncio.gather(*list(web._mail_tasks))


async def test_claim_mails_the_user(env, sent):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        assert (await c.post("/api/me/trial/claim", headers=bearer(keys["alice"]), json={"lang": "en"})).status_code == 200
    await flush()
    [(to, subject, body)] = sent
    assert to == "alice@example.com" and "free trial" in subject.lower()
    assert "1 hour" in body and "Lite" in body and "https://dash.example.com" in body and "10%" in body
    assert "Start now" in body and "https://dash.example.com/install.cmd" in body


async def test_claim_mail_in_persian(env, sent):
    gw, conn, cfg, ids, keys = env
    async with client(gw) as c:
        assert (await c.post("/api/me/trial/claim", headers=bearer(keys["alice"]), json={"lang": "fa"})).status_code == 200
    await flush()
    [(_, subject, body)] = sent
    assert "آزمایش رایگان" in subject and "کلاد" in body and "۱ ساعت" in body and "Claude Code" not in body


async def test_admin_grant_mails_in_the_last_order_language(env, sent):
    gw, conn, cfg, ids, keys = env
    conn.execute("INSERT INTO orders(user_id, name, email, tier, length, currency, quoted_amount, quoted_usd, quoted_rate, message, status, admin_mail, buyer_mail, created_at, "
                 "updated_at, lang) VALUES(?, 'alice', 'alice@example.com', 'lite', 'day', 'USD', 1, 1, 1, '', 'withdrawn', 'off', 'off', 1, 1, 'fa')",
                 (ids["alice"],))
    async with admin_client(gw) as c:
        assert (await c.post(f"/api/admin/users/{ids['alice']}/trial", json={"confirm": True})).status_code == 200
    await flush()
    [(to, subject, _)] = sent
    assert to == "alice@example.com" and "آزمایش رایگان" in subject


async def test_admin_grant_mails_in_english_without_orders(env, sent):
    gw, conn, cfg, ids, keys = env
    async with admin_client(gw) as c:
        assert (await c.post(f"/api/admin/users/{ids['alice']}/trial", json={})).status_code == 200
    await flush()
    [(_, subject, _)] = sent
    assert "free trial" in subject.lower()


async def test_no_address_no_mail(env, sent):
    gw, conn, cfg, ids, keys = env
    conn.execute("UPDATE users SET email=NULL WHERE id=?", (ids["alice"],))
    async with client(gw) as c:
        assert (await c.post("/api/me/trial/claim", headers=bearer(keys["alice"]), json={})).status_code == 200
    await flush()
    assert sent == []


async def test_no_mail_without_email_config(env, sent):
    gw, conn, cfg, ids, keys = env
    cfg.email = None
    async with client(gw) as c:
        assert (await c.post("/api/me/trial/claim", headers=bearer(keys["alice"]), json={})).status_code == 200
    await flush()
    assert sent == []


async def test_mail_failure_keeps_the_claim(env, sent, monkeypatch):
    gw, conn, cfg, ids, keys = env

    def down(*a, **k):
        raise OSError("smtp down")
    monkeypatch.setattr(mail, "send", down)
    async with client(gw) as c:
        assert (await c.post("/api/me/trial/claim", headers=bearer(keys["alice"]), json={})).status_code == 200
    await flush()
    assert conn.execute("SELECT COUNT(*) FROM trial_claims").fetchone()[0] == 1


async def test_no_discount_line_at_zero_follow_pct_and_minutes(env, sent):
    gw, conn, cfg, ids, keys = env
    trials.set_settings(conn, cfg, ids["admin"], {"follow_pct": 0, "duration_min": 90})
    async with client(gw) as c:
        assert (await c.post("/api/me/trial/claim", headers=bearer(keys["alice"]), json={})).status_code == 200
    await flush()
    [(_, _, body)] = sent
    assert "90 minutes" in body and "%" not in body
