"""Where an address is: city, region and country from ipinfo.io, looked up once per address (ip_geo), in the
background after a user is first seen there (db.note_ip). A failed lookup stores nothing; the address simply shows
without a place. Private, loopback and other non-public addresses are never sent."""
from __future__ import annotations

import ipaddress
import logging
import time

import httpx

URL = "https://ipinfo.io/{ip}/json"   # free tier without a token: rate-limited, enough for once per new address
TIMEOUT_S = 5.0

logger = logging.getLogger(__name__)
_pending: set[str] = set()   # addresses being looked up right now, so a burst of first requests asks once


def is_public(ip: str | None) -> bool:
    try:
        return bool(ip) and ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def known(conn, ip: str) -> bool:
    return conn.execute("SELECT 1 FROM ip_geo WHERE ip=?", (ip,)).fetchone() is not None


def store(conn, ip: str, place: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO ip_geo(ip, city, region, country, looked_up_at) VALUES(?,?,?,?,?)",
                 (ip, place.get("city"), place.get("region"), place["country"], int(time.time())))


async def locate(gw, ip: str) -> None:
    """Look `ip` up and keep its place, unless it is known, being looked up or not public. Never raises."""
    if not is_public(ip) or ip in _pending:
        return
    _pending.add(ip)
    try:
        if await gw.db.run(known, ip):
            return
        r = await gw.http.get(URL.format(ip=ip), timeout=TIMEOUT_S, headers={"accept": "application/json"})
        body = r.json() if r.status_code == 200 else {}
        country = body.get("country") if isinstance(body, dict) else None
        if not (isinstance(country, str) and len(country) == 2):
            logger.info("no place for %s (HTTP %s)", ip, r.status_code)
            return
        place = {k: (str(body[k])[:80] if body.get(k) else None) for k in ("city", "region")} | {"country": country.upper()}
        await gw.db.run(store, ip, place)
    except (httpx.HTTPError, ValueError) as e:
        logger.info("could not look up %s: %s", ip, e)
    except Exception:
        logger.exception("could not look up %s", ip)
    finally:
        _pending.discard(ip)
