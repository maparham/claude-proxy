"""Share limits are shares of the gateway's capacity: 100 − the bucket's unattributed points (2026-10-18 spec)."""
import pytest

from claude_proxy import tickets
from tests.test_tickets_capacity import add_user, grant
from tests.test_tickets_limits import DAY, NOW, RESET7, by_kind, check, env, piece, req, set_limit, snap  # noqa: F401
from tests.tickets_helpers import seeded


def outside(conn, bucket, util, resets, t=NOW - 500):
    """A rise with no gateway request before it: points the gateway can't pin on anyone."""
    snap(conn, t, util, bucket, resets)


@pytest.fixture
def plain(db):
    conn, cfg, ids = seeded(db)
    return conn, cfg, ids["alice"]


@pytest.mark.parametrize("value,limit", [(100, 95), (30, 28.5)])
def test_hand_set_share_7d_shrinks_by_outside_usage(plain, value, limit):
    conn, cfg, uid = plain
    set_limit(conn, uid, "share_7d", value, "pct")
    outside(conn, "7d", 5, RESET7, t=NOW - 600)    # 5 points used outside the gateway
    req(conn, uid, NOW - 300, i=1000)
    snap(conn, NOW - 100, 79, "7d", RESET7)         # alice alone moved it 74 points
    s = by_kind(conn, cfg, uid)["share_7d"]
    assert (s.value, s.limit, s.current) == (str(value), pytest.approx(limit), pytest.approx(74))
    if value == 100:
        assert s.pct == pytest.approx(74 / 95 * 100) and not s.exceeded
    else:
        assert s.exceeded and check(conn, cfg, uid).kind == "share_7d"


def test_hand_set_100_reaches_100_with_the_account(plain):
    conn, cfg, uid = plain
    set_limit(conn, uid, "share_7d", 100, "pct")
    outside(conn, "7d", 5, RESET7, t=NOW - 600)
    req(conn, uid, NOW - 300, i=1000)
    snap(conn, NOW - 100, 100, "7d", RESET7)
    s = by_kind(conn, cfg, uid)["share_7d"]
    assert s.pct == pytest.approx(100) and check(conn, cfg, uid).kind == "share_7d"


def test_hand_set_share_5h_uses_its_own_bucket(plain):
    conn, cfg, uid = plain
    set_limit(conn, uid, "share_5h", 50, "pct")
    set_limit(conn, uid, "share_7d", 50, "pct")
    outside(conn, "5h", 20, NOW + 3500)
    outside(conn, "7d", 0, RESET7)
    st = by_kind(conn, cfg, uid)
    assert (st["share_5h"].limit, st["share_7d"].limit) == (40, 50)


def test_capacity_is_full_again_once_the_window_has_reset(plain):
    conn, cfg, uid = plain
    set_limit(conn, uid, "share_5h", 50, "pct")
    outside(conn, "5h", 20, NOW - 10, t=NOW - 100)   # reset already passed
    assert by_kind(conn, cfg, uid)["share_5h"].limit == 50


def test_no_capacity_left_exceeds_every_share(plain):
    conn, cfg, uid = plain
    set_limit(conn, uid, "share_5h", 50, "pct")
    outside(conn, "5h", 100, NOW + 3500)
    s = by_kind(conn, cfg, uid)["share_5h"]
    assert (s.limit, s.current, s.exceeded, s.pct) == (0, 0, True, 100)
    assert check(conn, cfg, uid).kind == "share_5h"


def test_ticket_shares_shrink_too(env):
    conn, cfg, ids, _ = env
    snap(conn, NOW - 500, 20, "5h", NOW + 3500)   # env's snapshots read 0: these rises are outside usage
    snap(conn, NOW - 500, 10, "7d", RESET7)
    st = by_kind(conn, cfg, ids["alice"])
    assert st["share_5h"].limit == pytest.approx(5 * 0.8) and st["share_5h"].value == "5"
    assert st["share_7d"].limit == pytest.approx(piece(5, 3 * DAY + 300) * 0.9)


def test_stale_snapshot_in_an_unreset_window_still_sets_capacity(env):
    conn, cfg, ids, _ = env
    snap(conn, NOW - 7200, 30, "5h", NOW + 3500)   # stale, but its window has not reset: 30 points are still nobody's
    conn.execute("DELETE FROM quota_snapshots WHERE observed_at > ? AND bucket='5h'", (NOW - 7200,))
    st = by_kind(conn, cfg, ids["alice"])["share_5h"]
    assert st.limit == pytest.approx(5 * 0.7)


def test_selling_still_checks_max_sold_pct_not_runtime_capacity(env):
    conn, cfg, ids, _ = env
    snap(conn, NOW - 500, 50, "7d", RESET7)        # half the week used outside the gateway
    for n in range(3):
        add_user(conn, ids, f"u{n}")
        grant(conn, cfg, ids, who=f"u{n}", tier="standard", length="week")   # 5 + 3 × 25 = 80: the ceiling, not 50
    add_user(conn, ids, "bob")
    with pytest.raises(tickets.CapacityError):                                # 80 + 5 > 80
        grant(conn, cfg, ids, who="bob", tier="lite", length="week")
