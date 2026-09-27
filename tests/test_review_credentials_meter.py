"""Review fixes: refresh-rejection grouping, forced-refresh throttle, meter resilience to one bad line."""
import json

import httpx

from claude_proxy.gateway import Gateway
from claude_proxy.meter import SSEMeter
from tests.conftest import seed_oauth, sse_events


def _gateway(cfg, db, handler):
    return Gateway(cfg, db[1], http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _state(db):
    return db[1].execute("SELECT state FROM credentials WHERE backend='oauth'").fetchone()["state"]


# --- 1. `status in (400, 401) and ... or "invalid_grant" in body` grouping ---

async def test_429_mentioning_invalid_grant_backs_off_instead_of_needs_login(cfg, db):
    seed_oauth(db[1], access="old", refresh="r1")
    gw = _gateway(cfg, db, lambda req: httpx.Response(
        429, json={"error": {"type": "rate_limit_error", "message": "too many invalid_grant attempts; slow down"}}))
    assert not await gw.backend.on_unauthorized("old")
    assert _state(db) == "active"
    assert gw.backend._retry_at > 0 and "429" in gw.backend.last_error


async def test_503_mentioning_invalid_grant_backs_off_instead_of_needs_login(cfg, db):
    seed_oauth(db[1], access="old", refresh="r1")
    gw = _gateway(cfg, db, lambda req: httpx.Response(503, text="upstream invalid_grant handler unavailable"))
    assert not await gw.backend.on_unauthorized("old")
    assert _state(db) == "active"


async def test_400_invalid_grant_still_marks_needs_login(cfg, db):
    seed_oauth(db[1], access="old", refresh="r1")
    gw = _gateway(cfg, db, lambda req: httpx.Response(400, json={"error": "invalid_grant"}))
    assert not await gw.backend.on_unauthorized("old")
    assert _state(db) == "needs_login"


async def test_400_rate_limit_backs_off(cfg, db):
    seed_oauth(db[1], access="old", refresh="r1")
    gw = _gateway(cfg, db, lambda req: httpx.Response(400, json={"error": "rate_limit_exceeded"}))
    assert not await gw.backend.on_unauthorized("old")
    assert _state(db) == "active"


# --- 2. A forced refresh right after a successful one must not spend another refresh token ---

async def test_forced_refresh_is_a_no_op_within_60s_of_the_last_one(cfg, db):
    seed_oauth(db[1], access="old", refresh="r1")
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"access_token": f"new{len(calls)}", "refresh_token": f"r{len(calls) + 1}", "expires_in": 28800})

    gw = _gateway(cfg, db, handler)
    assert await gw.backend.on_unauthorized("old")
    assert gw.backend.current_token() == "new1"
    # The fresh token is rejected again (e.g. a scope-rejected path): no second refresh within the window.
    assert await gw.backend.on_unauthorized("new1")
    assert await gw.backend.on_unauthorized("new1")
    assert len(calls) == 1
    assert gw.backend.current_token() == "new1"


async def test_forced_refresh_runs_again_once_the_window_has_passed(cfg, db, monkeypatch):
    import time as _t
    seed_oauth(db[1], access="old", refresh="r1")
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"access_token": f"new{len(calls)}", "refresh_token": f"r{len(calls) + 1}", "expires_in": 28800})

    gw = _gateway(cfg, db, handler)
    assert await gw.backend.on_unauthorized("old")
    now = _t.time()
    monkeypatch.setattr("claude_proxy.credentials.time.time", lambda: now + 61)
    assert await gw.backend.on_unauthorized("new1")
    assert len(calls) == 2 and calls[1]["refresh_token"] == "r2"
    assert gw.backend.current_token() == "new2"


async def test_first_forced_refresh_after_login_is_not_throttled(cfg, db):
    seed_oauth(db[1], access="old", refresh="r1")
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2", "expires_in": 28800})

    gw = _gateway(cfg, db, handler)
    assert await gw.backend.on_unauthorized("old")
    assert len(calls) == 1


# --- 3. One unparsable data: line must not stop metering ---

def test_garbage_data_line_does_not_stop_metering():
    events = sse_events(output_tokens=64, text="Hello world, this is streamed")
    garbage = b"event: content_block_delta\ndata: {not json at all\n\n"
    head, tail = events.split(b"event: message_delta", 1)
    m = SSEMeter()
    m.feed(head + garbage + b"event: message_delta" + tail)
    r = m.finalize()
    assert r.meter_error and r.meter_error_detail.startswith("json_error")
    assert r.output_tokens == 64
    assert r.complete


def test_garbage_line_before_cut_off_still_estimates_from_later_streamed_chars():
    text = "x" * 3500
    events = sse_events(output_tokens=1000, text=text, stop=False)
    cut = events[:events.index(b"event: message_delta")]
    first_delta = cut.index(b"event: content_block_delta")
    garbage = b"data: <<<garbage>>>\n\n"
    m = SSEMeter()
    m.feed(cut[:first_delta] + garbage + cut[first_delta:])
    r = m.finalize()
    assert r.meter_error
    assert r.output_tokens == 1000   # every content_block_delta after the bad line was still counted


def test_line_cap_still_stops_metering():
    m = SSEMeter()
    m.feed(b"data: " + b"x" * (1_048_576 + 10) + b"\n\n")
    m.feed(sse_events(output_tokens=64))
    r = m.finalize()
    assert r.meter_error and r.meter_error_detail == "event_cap"
    assert r.output_tokens == 0 and not r.complete
