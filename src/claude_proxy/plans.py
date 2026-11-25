"""Subscription plans: what plan an account is on, and how many Pro weeks one of its points is worth.

Passes are sized in Pro weeks so they can move between accounts of different plans; a plan's factor says how many Pro
weeks a full account of that plan holds (separately for the weekly and the 5-hour window). Imports no `db`: db._migrate
imports from here, so functions take a connection and just run SQL.
"""
from __future__ import annotations

import json
import math
import time

PLANS = ("pro", "max_5x", "max_20x")
PLAN_LABELS = {"pro": "Pro", "max_5x": "Max 5x", "max_20x": "Max 20x"}
DEFAULT_FACTORS = {"pro": {"7d": 1.0, "5h": 1.0}, "max_5x": {"7d": 5.0, "5h": 5.0}, "max_20x": {"7d": 20.0, "5h": 20.0}}


def detect(organization_type: str | None, rate_limit_tier: str | None) -> str | None:
    """The plan Anthropic's profile names; a bare 'max' (no multiplier) or anything unknown is None."""
    for s in (rate_limit_tier, organization_type):
        s = (s or "").lower()
        if "20x" in s:
            return "max_20x"
        if "5x" in s:
            return "max_5x"
        if "pro" in s:
            return "pro"
    return None


def _audit(conn, actor: int | None, action: str, target: str, detail: dict | None = None) -> None:
    conn.execute("INSERT INTO audit_log(at, actor_user_id, action, target, detail_json) VALUES(?,?,?,?,?)",
                 (int(time.time()), actor, action, target, json.dumps(detail) if detail else None))


def _get(conn, key: str) -> str | None:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def _put(conn, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        conn.execute("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))


def factors(conn) -> dict[str, dict[str, float]]:
    out = {p: dict(f) for p, f in DEFAULT_FACTORS.items()}
    try:
        stored = json.loads(_get(conn, "plan_factors") or "{}")
    except ValueError:
        stored = {}
    for p, f in (stored.items() if isinstance(stored, dict) else ()):
        if p in out and isinstance(f, dict):
            for b in ("7d", "5h"):
                if isinstance(f.get(b), (int, float)) and f[b] > 0:
                    out[p][b] = float(f[b])
    return out


def set_factor(conn, plan: str, f_7d, f_5h, actor: int | None) -> dict:
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan!r}")
    vals = []
    for v in (f_7d, f_5h):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 < v <= 1000:
            raise ValueError("a factor must be a number above 0 and at most 1000")
        vals.append(float(v))
    f = factors(conn)
    f[plan] = {"7d": vals[0], "5h": vals[1]}
    _put(conn, "plan_factors", json.dumps(f))
    _audit(conn, actor, "plan_factor_set", plan, {"7d": vals[0], "5h": vals[1]})
    return f


def _latest(conn, account: str):
    return conn.execute("SELECT observed_at, organization_type, rate_limit_tier FROM account_plans WHERE account=? "
                        "ORDER BY observed_at DESC, id DESC LIMIT 1", (account,)).fetchone()


def detected(conn, account: str) -> dict | None:
    r = _latest(conn, account)
    if not r:
        return None
    return {"observed_at": r[0], "organization_type": r[1], "rate_limit_tier": r[2], "plan": detect(r[1], r[2])}


def override(conn, account: str) -> str | None:
    v = _get(conn, f"account_plan:{account}")
    return v if v in PLANS else None


def set_override(conn, account: str, plan: str | None, actor: int | None) -> str | None:
    if plan is not None and plan not in PLANS:
        raise ValueError(f"unknown plan {plan!r}")
    _put(conn, f"account_plan:{account}", plan)
    _audit(conn, actor, "plan_override", account, {"plan": plan})
    return plan


def plan(conn, account: str) -> str | None:
    return override(conn, account) or (detected(conn, account) or {}).get("plan")


def factor(conn, account: str, bucket: str = "7d") -> float | None:
    p = plan(conn, account)
    return factors(conn)[p][bucket] if p else None


def shares(conn, account: str, size: float, size_5h: float | None) -> tuple[float, float] | None:
    """A pass of `size` Pro weeks as percent of the account's weekly and 5-hour windows; the 5-hour size defaults to size."""
    p = plan(conn, account)
    if not p:
        return None
    f = factors(conn)[p]
    return size * 100 / f["7d"], (size if size_5h is None else size_5h) * 100 / f["5h"]


def pro_weeks(conn, account: str, points: float, bucket: str = "7d") -> float | None:
    f = factor(conn, account, bucket)
    return None if f is None else points * f / 100


def _tier(d: dict | None) -> str | None:
    return (d["rate_limit_tier"] or d["organization_type"]) if d else None


def acknowledged(conn, account: str) -> str | None:
    return _get(conn, f"account_plan_ack:{account}")


def acknowledge(conn, account: str, actor: int | None) -> None:
    tier = _tier(detected(conn, account))
    if tier is None:
        return
    _put(conn, f"account_plan_ack:{account}", tier)
    _audit(conn, actor, "plan_ack", account, {"tier": tier})


def pending_change(conn, account: str) -> dict | None:
    """The plan change the admin has not acknowledged yet. An account never acknowledged has nothing to compare to."""
    ack, latest = acknowledged(conn, account), _tier(detected(conn, account))
    if ack is None or latest is None or ack == latest:
        return None
    f = factors(conn)
    new, old = detect(latest, latest), detect(ack, ack)
    down = new is None or (old is not None and f[new]["7d"] < f[old]["7d"])
    # from_plan/to_plan: the plans the tiers name (None when unknown), for the dashboard's labels.
    return {"from": ack, "to": latest, "from_plan": old, "to_plan": new, "downgrade": down}


def sells(conn, account: str) -> str | None:
    """None when the account may sell passes, else why not."""
    if plan(conn, account) is None:
        return "plan_unknown"
    if pending_change(conn, account):
        return "plan_changed"
    return None
