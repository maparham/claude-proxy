"""ZarinPal client (payments design, section 4): request, verify and the StartPay address, against a fake server."""
import json

import httpx
import pytest

from claude_proxy import zarinpal
from claude_proxy.config import Config, ConfigError

MID = "00000000-0000-0000-0000-000000000000"


def fake(monkeypatch, handler):
    """Route the client's calls to `handler(request) -> httpx.Response`; returns the list of (url, json body) seen."""
    seen = []

    def wrapped(req):
        seen.append((str(req.url), json.loads(req.content)))
        return handler(req)
    monkeypatch.setattr(zarinpal, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    return seen


async def test_request_returns_the_authority(monkeypatch):
    seen = fake(monkeypatch, lambda r: httpx.Response(200, json={"data": {"code": 100, "authority": "A0001", "fee": 0}, "errors": []}))
    a = await zarinpal.request(MID, 1250000, "Lite week", "https://rahkar.pro/pay/callback", "a@b.c", 7, sandbox=False)
    assert a == "A0001"
    url, body = seen[0]
    assert url == "https://payment.zarinpal.com/pg/v4/payment/request.json"
    assert body == {"merchant_id": MID, "amount": 1250000, "currency": "IRT", "description": "Lite week",
                    "callback_url": "https://rahkar.pro/pay/callback", "metadata": {"email": "a@b.c", "order_id": "7"}}


async def test_sandbox_uses_the_sandbox_host(monkeypatch):
    seen = fake(monkeypatch, lambda r: httpx.Response(200, json={"data": {"code": 100, "authority": "S1"}, "errors": []}))
    await zarinpal.request(MID, 1000, "d", "https://x/cb", "a@b.c", 1, sandbox=True)
    assert seen[0][0] == "https://sandbox.zarinpal.com/pg/v4/payment/request.json"
    assert zarinpal.start_url("S1", sandbox=True) == "https://sandbox.zarinpal.com/pg/StartPay/S1"
    assert zarinpal.start_url("A1", sandbox=False) == "https://payment.zarinpal.com/pg/StartPay/A1"


async def test_refusal_carries_code_and_message(monkeypatch):
    fake(monkeypatch, lambda r: httpx.Response(200, json={"data": [], "errors": {"code": -9, "message": "The input params invalid"}}))
    with pytest.raises(zarinpal.ZarinpalRefused) as e:
        await zarinpal.request(MID, 1000, "d", "https://x/cb", "a@b.c", 1, sandbox=False)
    assert (e.value.code, e.value.message) == (-9, "The input params invalid")


@pytest.mark.parametrize("handler", [lambda r: httpx.Response(502, text="bad gateway"),
                                     lambda r: httpx.Response(200, text="<html>"),
                                     lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow"))])
async def test_unreachable_or_garbled_is_unavailable(monkeypatch, handler):
    fake(monkeypatch, handler)
    with pytest.raises(zarinpal.ZarinpalUnavailable):
        await zarinpal.verify(MID, 1000, "A1", sandbox=False)


async def test_verify_100_and_101(monkeypatch):
    for code in (100, 101):
        seen = fake(monkeypatch, lambda r, c=code: httpx.Response(200, json={"data": {"code": c, "ref_id": 201, "card_pan": "502229******5995",
                                                                                       "card_hash": "X", "fee": 0}, "errors": []}))
        v = await zarinpal.verify(MID, 1250000, "A1", sandbox=False)
        assert v == {"code": code, "ref_id": 201, "card_pan": "502229******5995"}
        assert seen[0] == ("https://payment.zarinpal.com/pg/v4/payment/verify.json", {"merchant_id": MID, "amount": 1250000, "authority": "A1"})


def test_config_section_and_secret(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text("[zarinpal]\nsandbox = true\n")
    monkeypatch.setenv("ZARINPAL_MERCHANT_ID", MID)
    cfg = Config.load(str(p))
    assert cfg.zarinpal.sandbox is True and cfg.zarinpal.merchant_id() == MID
    assert Config().zarinpal is None
    p.write_text("[zarinpal]\nmerchant_id = \"x\"\n")
    with pytest.raises(ConfigError, match=r"\[zarinpal\]"):
        Config.load(str(p))
