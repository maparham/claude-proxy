"""ZarinPal payment gateway client (payments design, section 4): request a payment, verify it, the StartPay address.
https://www.zarinpal.com/docs/paymentGateway/connectToGateway.html"""
from __future__ import annotations

import httpx

TIMEOUT_S = 10


class ZarinpalUnavailable(Exception):
    """ZarinPal could not be asked (network error, timeout, 5xx or an unreadable answer): try again later."""


class ZarinpalRefused(Exception):
    """ZarinPal answered with an error code (bad merchant, amount, authority, an unpaid payment...)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def _host(sandbox: bool) -> str:
    return "https://sandbox.zarinpal.com" if sandbox else "https://payment.zarinpal.com"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S)


def start_url(authority: str, sandbox: bool) -> str:
    return f"{_host(sandbox)}/pg/StartPay/{authority}"


async def _call(path: str, body: dict, sandbox: bool) -> dict:
    """The `data` object of a successful answer; ZarinpalRefused for an error code, ZarinpalUnavailable otherwise."""
    try:
        async with _client() as c:
            r = await c.post(f"{_host(sandbox)}/pg/v4/payment/{path}", json=body, headers={"Accept": "application/json"})
        if r.status_code >= 500:
            raise ZarinpalUnavailable(f"{path} answered {r.status_code}")
        out = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ZarinpalUnavailable(f"{path}: {e}") from e
    data, errors = (out.get("data"), out.get("errors")) if isinstance(out, dict) else (None, None)
    if isinstance(data, dict) and data.get("code") in (100, 101):
        return data
    err = errors if isinstance(errors, dict) and "code" in errors else data if isinstance(data, dict) else {}
    if "code" not in err:
        raise ZarinpalUnavailable(f"{path}: unreadable answer")
    raise ZarinpalRefused(int(err["code"]), str(err.get("message") or ""))


async def request(merchant_id: str, amount: int, description: str, callback_url: str, email: str, order_id: int,
                  sandbox: bool) -> str:
    """Open a payment of `amount` Toman; returns its authority."""
    data = await _call("request.json", {"merchant_id": merchant_id, "amount": int(amount), "currency": "IRT",
                                        "description": description, "callback_url": callback_url,
                                        "metadata": {"email": email, "order_id": str(order_id)}}, sandbox)
    return str(data["authority"])


async def verify(merchant_id: str, amount: int, authority: str, sandbox: bool) -> dict:
    """Confirm a payment the buyer came back from with Status=OK. Code 100 the first time, 101 after."""
    data = await _call("verify.json", {"merchant_id": merchant_id, "amount": int(amount), "authority": authority}, sandbox)
    return {"code": data["code"], "ref_id": data.get("ref_id"), "card_pan": data.get("card_pan")}
