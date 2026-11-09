"""What a user's pass card shows (GET /api/me/tickets), built in one place so that the dashboard and the Telegram
bot's "My passes" always agree (Telegram bot design, section 6). Moved unchanged from the route, side effect
included: used-up passes give way to a queued one here (limits.hand_over), as they do on a request."""
from __future__ import annotations

from . import capacity, limits, payments, refunds, tickets


def start_offer(conn, cfg, user_id: int, now: float) -> dict:
    """Shared with the Telegram bot (design 2026-11-05, section 6), so a new ticket's start never disagrees with the
    dashboard's order dialog: hand a used-up running ticket to its queued one first (as `build` does, side effect
    included), then `next_start` and `replaces` (`tickets.next_start`'s own meaning), and `offer`, whether the order
    dialog's After the current one / Now choice applies at all (a current pass that `next_start` would not replace,
    exactly `tk.current && !tk.replaces` in app.js)."""
    limits.hand_over(conn, cfg, user_id, now)
    next_start, replaces = tickets.next_start(conn, cfg, user_id, now)
    replaces = bool(replaces)
    offer = tickets.covering(conn, user_id, now) is not None and not replaces
    return {"offer": offer, "next_start": next_start, "replaces": replaces}


def build(conn, cfg, user_id: int, now: float) -> dict:
    """The card for an enabled gateway; the caller checks cfg.tickets.enabled."""
    so = start_offer(conn, cfg, user_id, now)   # used-up tickets give way to a queued one here too, not only on a request
    st = tickets.user_state(conn, user_id, now)
    label = lambda t: cfg.tickets.tiers[t["tier"]].label if t["tier"] in cfg.tickets.tiers else t["tier"]  # noqa: E731
    # No ticket ids: they number every sale on the account.
    # `manual`: whether the order dialog offers an unpaid order beside ZarinPal.
    mode = payments.pay_mode(conn, cfg)
    out = {"enabled": True, "gated": st["gated"], "current": None, "queued": None, "manual": payments.manual_orders(mode)}
    cur = st["current"]
    past = tickets.past(conn, user_id, now)
    # The tokens each pass served so far, the running and past ones in one query.
    served = capacity.pass_tokens(conn, cfg, [*([cur] if cur else []), *tickets.running(conn, user_id, now), *past], now)
    if cur:
        ups = [b for b in tickets.active_bonuses(conn, cur["id"], now) if b["tier"] is not None]
        active = [b for b in tickets.active_bonuses(conn, cur["id"], now) if b["tier"] is None]   # gifts; upgrades were paid for
        week = tickets.week_bounds(conn, cfg, now)
        week_end = week[1] if week is not None else tickets.current_day(cur, now)[1]   # display-only fallback; enforcement is 503 then
        # No subscription percents: the user never learns about the account behind the gateway, so a bonus reads
        # as a percent of the ticket's own allowance (+2 points on a 5% ticket: +40%).
        share_5h = cur["share_5h_pct"] if cur["share_5h_pct"] is not None else cur["share_pct"]
        now_tier = tickets.tier_now(conn, cur)
        out["current"] = {"tier": now_tier, "label": label({"tier": now_tier}), "starts_at": cur["starts_at"], "length": cur["length"],
                          "upgraded_from": {"tier": cur["tier"], "label": label(cur)} if ups else None,
                          "ends_at": cur["ends_at"], "effective_end": cur["effective_end"],
                          "bonus_days": (cur["effective_end"] - cur["ends_at"]) // tickets.DAY, "week_end": week_end,
                          "week_known": week is not None,
                          # Nothing left before it ends: the card offers a new ticket, which starts at once.
                          "used_up": limits.used_up(conn, cfg, user_id, now),
                          "bonus_pct": (tickets.bonus_share(conn, cur["id"], now) - sum(
                              b["share_5h_pct"] if b["share_5h_pct"] is not None else b["share_pct"] for b in ups)) / share_5h * 100,
                          "bonuses": [{"pct": b["share_pct"] / cur["share_pct"] * 100, "note": b["note"], "ends_at": b["ends_at"]}
                                      for b in active]}
        # Extra days with their note; a bonus that also adds a share running now is shown with that share instead.
        shown = {b["id"] for b in active}
        # Passes running side by side, each with limits of its own (2026-10-28): which one serves now, which the
        # user pinned, and how much of each limit it has used. Its place in the list names one to pin (no ticket ids).
        run = tickets.running(conn, user_id, now)
        # `held`: no pass serves, and this one is next — the pinned one at a limit, or else the one back soonest.
        picked, serving_sts, serves = limits.pick(conn, cfg, user_id, now, run)
        serving = picked if serves else None
        pin = conn.execute("SELECT pass_pin FROM users WHERE id=?", (user_id,)).fetchone()[0]
        def pass_use(t) -> dict:
            """A running pass's use of each limit, and at one: when it is back (`back_at`, the last of its limits
            to refill), or `used_up` when that is after it ends."""
            # The serving pass as it serves, which may be borrowing from its next part (2026-10-30 spec, §4).
            sts = serving_sts if picked is not None and t["id"] == picked["id"] else limits.pass_states(conn, cfg, t, now)
            out = {f"pct_{s.kind[6:]}": s.pct for s in sts}
            whole = limits.pass_weeks(conn, cfg, t, now, next((s for s in sts if s.kind == "share_7d"), None))
            out["pct_pass"] = whole["ticket_pct"] if whole else out.get("pct_7d")   # the whole pass's use, as its card reads
            hit = [s.resets_at or (now + s.reset_in if s.reset_in else None) for s in sts if s.exceeded]
            if hit:
                out |= {"back_at": None if None in hit else max(hit), "used_up": limits.gone(t, sts, now)}
                # each reached limit's own reset, for the ! on its row
                out |= {f"reset_{s.kind[6:]}": s.resets_at or (now + s.reset_in if s.reset_in else None) for s in sts if s.exceeded}
            return out

        # With more than one live pass the card lists them all, the queued ones after the running ones (`queued`).
        later = [t for t in tickets.user_tickets(conn, user_id) if t["cancelled_at"] is None and t["starts_at"] > now]
        uses = {t["id"]: pass_use(t) for t in run}
        out["current"]["tokens"] = served[cur["id"]]
        out["current"]["passes"] = [] if len(run) + len(later) < 2 else [
            {"tier": (x := tickets.tier_now(conn, t)), "label": label({"tier": x}), "starts_at": t["starts_at"], "length": t["length"],
             "bought_at": t["granted_at"], "effective_end": t["effective_end"], "serving": serving is not None and t["id"] == serving["id"],
             "held": serving is None and picked is not None and t["id"] == picked["id"] and not uses[t["id"]].get("used_up"),
             "pinned": t["id"] == pin, "tokens": served[t["id"]], **uses[t["id"]]} for t in run] + [{"tier": t["tier"], "label": label(t), "length": t["length"], "starts_at": t["starts_at"],
                              "bought_at": t["granted_at"], "effective_end": t["effective_end"], "queued": True} for t in later]
        out["current"]["serving_end"] = (serving or cur)["effective_end"]
        out["current"]["day_bonuses"] = [{"extra_days": b["extra_days"], "note": b["note"]} for b in tickets.bonuses(conn, cur["id"])
                                         if b["cancelled_at"] is None and b["extra_days"] > 0 and b["id"] not in shown]
    if st["queued"]:
        q = st["queued"]
        out["queued"] = {"tier": q["tier"], "label": label(q), "starts_at": q["starts_at"], "effective_end": q["effective_end"]}
    out["past"] = [{"tier": t["tier"], "label": label(t), "how": t["how"], "starts_at": t["starts_at"], "ended_at": t["ended_at"],
                    "amount": t["amount"], "currency": t["currency"], "tokens": served[t["id"]]} for t in past]
    # The gateway's own currency (IRT), as on the home page, whatever the running pass was bought in; the dashboard
    # offers USD beside it. Not the running pass's currency: a pass granted in EUR turned the whole list into euros.
    currency = next(iter(cfg.tickets.currencies), "USD")
    # A new ticket queues after the last, or starts now in place of a used-up one: the order dialog's "after" date.
    # `replaces`: the latter, so the dialog offers no start choice. Not merely `used_up`: with a ticket queued that
    # had no room to move up (hand_over), a new one still queues after it.
    out["next_start"], out["replaces"] = so["next_start"], so["replaces"]
    out["prices"] = tickets.price_table(conn, cfg, now, currency, starts_at=out["next_start"],
                                        exclude=[t["id"] for t in tickets.next_start(conn, cfg, user_id, now, True)[1]])
    # A ticket can also start now, beside the running one: the order dialog's terms for that choice.
    out["split_now"] = tickets.order_split(conn, cfg, now, now) if cur else None
    out["timeline"] = tickets.timeline(conn, cfg, user_id, now)   # the card's chart, with more than one live ticket
    out["now"] = now   # discount countdowns run on the server's clock, as on /pricing
    out["terms_version"] = tickets.TERMS_VERSION
    # Passes paid through ZarinPal in the last 15 minutes and not used yet: the buyer may cancel them for the money back.
    out["refunds"] = refunds.offers(conn, cfg, user_id, now)
    # Online payment (payments design, section 4): offered by the Payment selector alone. The Toman prices are None
    # without an IRT rate (the button then has no amount), and may be stale: paying is refused then.
    out["pay"] = ({"currency": payments.CURRENCY, "prices": payments.prices(conn, cfg, now), "vat_percent": cfg.zarinpal.vat_percent}
                  if payments.online(mode) else None)
    return out
