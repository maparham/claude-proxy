"""Free trial pass (design 2026-11-14): settings, claims, starting, the request path."""
import pytest

from claude_proxy import limits, passcard, tickets, trials
from claude_proxy.db import create_user, delete_user, init_db, revoke
from tests.tickets_helpers import DAY, NOW, RESET7, live, seeded, user


@pytest.fixture
def env(tmp_path):
    conn = init_db(str(tmp_path / "test.db"))
    conn, cfg, ids = seeded((None, conn))
    live(conn, NOW)
    trials.set_settings(conn, cfg, ids["admin"], {"enabled": True})
    yield conn, cfg, ids
    conn.close()


def new_user(conn, name):
    uid, _ = create_user(conn, name)
    conn.execute("UPDATE users SET email=? WHERE id=?", (name, uid))
    return uid


def test_defaults_follow_the_smallest_tier(env):
    conn, cfg, _ = env
    s = trials.settings(conn, cfg)
    assert (s.duration_min, s.valid_hours, s.daily_cap, s.follow_pct, s.follow_days) == (60, 24, 5, 10, 7)
    assert s.tier == "lite"
    assert s.share_pct == tickets.effective_share(conn, cfg, "lite")
    assert s.share_5h_pct == tickets.effective_share_5h(conn, cfg, "lite")


def test_settings_validate(env):
    conn, cfg, ids = env
    for bad in ({"duration_min": 0}, {"valid_hours": 1.5}, {"share_pct": 120}, {"follow_pct": 95}, {"tier": "gold"}, {"nope": 1},
                {"enabled": "yes"}):
        with pytest.raises(trials.TrialError):
            trials.set_settings(conn, cfg, ids["admin"], bad)
    s = trials.set_settings(conn, cfg, ids["admin"], {"duration_min": 90, "share_pct": 2.5})
    assert (s.duration_min, s.share_pct) == (90, 2.5)
    assert trials.set_settings(conn, cfg, ids["admin"], {"share_pct": None}).share_pct == tickets.effective_share(conn, cfg, "lite")


def test_claim_once(env):
    conn, cfg, ids = env
    c = trials.claim(conn, cfg, user(conn, ids["alice"]), NOW)
    assert c["expires_at"] == NOW + 24 * 3600 and c["granted_by"] is None
    with pytest.raises(trials.TrialError, match="already had"):
        trials.claim(conn, cfg, user(conn, ids["alice"]), NOW + 1)
    assert trials.offer(conn, cfg, user(conn, ids["alice"]), NOW)["claim"]["expires_at"] == c["expires_at"]


def test_claim_refused_when_off_or_after_a_pass(env):
    conn, cfg, ids = env
    trials.set_settings(conn, cfg, ids["admin"], {"enabled": False})
    assert trials.offer(conn, cfg, user(conn, ids["alice"]), NOW)["reason"] == "off"
    trials.set_settings(conn, cfg, ids["admin"], {"enabled": True})
    tickets.grant(conn, cfg, ids["admin"], user(conn, ids["alice"]), "lite", "day", "EUR", terms_version=tickets.TERMS_VERSION, now=NOW)
    with pytest.raises(trials.TrialError, match="new users"):
        trials.claim(conn, cfg, user(conn, ids["alice"]), NOW)


def test_same_email_after_delete_gets_no_second_trial(env):
    conn, cfg, ids = env
    bob = new_user(conn, "bob@example.com")
    trials.claim(conn, cfg, user(conn, bob), NOW)
    revoke(conn, bob)
    delete_user(conn, bob, now=NOW)
    bob2 = new_user(conn, "Bob@example.com")
    assert trials.offer(conn, cfg, user(conn, bob2), NOW)["reason"] == "used"


def test_daily_cap_counts_self_claims_in_24h(env):
    conn, cfg, ids = env
    trials.set_settings(conn, cfg, ids["admin"], {"daily_cap": 2})
    a, b, c = (new_user(conn, f"u{i}@x") for i in range(3))
    trials.admin_grant(conn, cfg, ids["admin"], user(conn, a), NOW)   # exempt
    trials.claim(conn, cfg, user(conn, b), NOW)
    trials.claim(conn, cfg, user(conn, ids["alice"]), NOW)
    with pytest.raises(trials.TrialError, match="all taken"):
        trials.claim(conn, cfg, user(conn, c), NOW)
    assert trials.public_offer(conn, cfg, NOW) is None
    trials.claim(conn, cfg, user(conn, c), NOW + DAY + 1)
    assert trials.public_offer(conn, cfg, NOW + DAY + 1) is not None   # only c's claim is within the last 24 hours


def test_start_makes_a_trial_pass(env):
    conn, cfg, ids = env
    alice = user(conn, ids["alice"])
    conn.execute("INSERT INTO limits(user_id, kind, scope, value, unit, updated_at) VALUES(?,?,?,?,?,?)",
                 (alice["id"], "cost_total", "*", "0", "usd", NOW))
    trials.claim(conn, cfg, alice, NOW)
    t = trials.start(conn, cfg, alice["id"], NOW + 600)
    assert (t["trial"], t["tier"], t["usd"], t["days"]) == (1, "lite", 0, 0)
    assert (t["starts_at"], t["ends_at"]) == (NOW + 600, NOW + 600 + 3600)
    assert t["share_pct"] == tickets.effective_share(conn, cfg, "lite")
    assert conn.execute("SELECT 1 FROM limits WHERE user_id=? AND kind='cost_total'", (alice["id"],)).fetchone() is None
    unit = conn.execute("SELECT pct, expires_at, state FROM referral_units WHERE user_id=?", (alice["id"],)).fetchone()
    assert tuple(unit) == (10, NOW + 600 + 3600 + 7 * DAY, "saved")
    assert trials.open_claim(conn, alice["id"], NOW + 601) is None
    with pytest.raises(trials.TrialError):
        trials.start(conn, cfg, alice["id"], NOW + 700)


def test_lapsed_claim_cannot_start_and_counts(env):
    conn, cfg, ids = env
    alice = user(conn, ids["alice"])
    trials.claim(conn, cfg, alice, NOW)
    with pytest.raises(trials.TrialError, match="no free trial"):
        trials.start(conn, cfg, alice["id"], NOW + 24 * 3600)
    assert trials.offer(conn, cfg, alice, NOW + 24 * 3600)["reason"] == "used"


def test_no_room_keeps_the_claim(env, monkeypatch):
    conn, cfg, ids = env
    alice = user(conn, ids["alice"])
    trials.claim(conn, cfg, alice, NOW)
    from claude_proxy import capacity

    def full(*a, **k):
        raise tickets.CapacityError("full")
    monkeypatch.setattr(capacity, "check", full)
    with pytest.raises(trials.TrialError, match="can't start"):
        trials.start(conn, cfg, alice["id"], NOW)
    assert trials.open_claim(conn, alice["id"], NOW) is not None
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_admin_grant_needs_confirm_after_a_pass(env):
    conn, cfg, ids = env
    alice = user(conn, ids["alice"])
    tickets.grant(conn, cfg, ids["admin"], alice, "lite", "day", "EUR", terms_version=tickets.TERMS_VERSION, now=NOW)
    with pytest.raises(trials.TrialError, match="confirm"):
        trials.admin_grant(conn, cfg, ids["admin"], alice, NOW)
    assert conn.execute("SELECT COUNT(*) FROM trial_claims").fetchone()[0] == 0
    c = trials.admin_grant(conn, cfg, ids["admin"], alice, NOW, confirm=True)
    assert c["granted_by"] == ids["admin"]
    with pytest.raises(trials.TrialError, match="waiting"):
        trials.admin_grant(conn, cfg, ids["admin"], alice, NOW, confirm=True)


def test_trial_cannot_be_upgraded(env):
    conn, cfg, ids = env
    trials.claim(conn, cfg, user(conn, ids["alice"]), NOW)
    t = trials.start(conn, cfg, ids["alice"], NOW)
    with pytest.raises(tickets.TicketError, match="free trial"):
        tickets.upgrade_quote(conn, cfg, t, "standard", "EUR", NOW + 60)


# ---------- the request path ----------

def fresh(conn, t):
    """5-hour and 7-day snapshots at `t`, so a pass's limits are measured rather than refused for want of data."""
    for bucket, resets in (("5h", t + 3600), ("7d", RESET7)):
        conn.execute("INSERT INTO quota_snapshots(observed_at, source, bucket, utilization_pct, resets_at) VALUES(?,?,?,?,?)",
                     (t, "header", bucket, 0, resets))


def test_first_request_starts_the_claim(env):
    conn, cfg, ids = env
    alice = ids["alice"]
    trials.claim(conn, cfg, user(conn, alice), NOW)
    served = {}
    fresh(conn, NOW + 30)
    assert limits.evaluate(conn, cfg, alice, "claude-sonnet-5", "/v1/messages", now=NOW + 60, served=served) is None
    t = tickets.get(conn, served["ticket_id"])
    assert t["trial"] == 1 and t["starts_at"] == NOW + 60


def test_count_tokens_does_not_start_it(env):
    conn, cfg, ids = env
    alice = ids["alice"]
    trials.claim(conn, cfg, user(conn, alice), NOW)
    limits.evaluate(conn, cfg, alice, "claude-sonnet-5", limits.COUNT_TOKENS_PATH, now=NOW + 60)
    assert trials.open_claim(conn, alice, NOW + 61) is not None


def test_running_pass_keeps_the_claim_waiting(env):
    conn, cfg, ids = env
    alice = user(conn, ids["alice"])
    tickets.grant(conn, cfg, ids["admin"], alice, "lite", "day", "EUR", terms_version=tickets.TERMS_VERSION, now=NOW)
    trials.admin_grant(conn, cfg, ids["admin"], alice, NOW, confirm=True)
    fresh(conn, NOW + 30)
    assert limits.evaluate(conn, cfg, alice["id"], "claude-sonnet-5", "/v1/messages", now=NOW + 60) is None
    assert trials.open_claim(conn, alice["id"], NOW + 61) is not None


def test_pass_card_marks_the_trial(env):
    conn, cfg, ids = env
    trials.claim(conn, cfg, user(conn, ids["alice"]), NOW)
    trials.start(conn, cfg, ids["alice"], NOW)
    card = passcard.build(conn, cfg, ids["alice"], NOW + 60)
    assert card["current"]["trial"] is True
    card = passcard.build(conn, cfg, ids["alice"], NOW + 2 * 3600)
    assert card["past"][0]["trial"] is True
