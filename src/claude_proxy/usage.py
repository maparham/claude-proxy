"""Aggregation over the `requests` ledger.

Every token total in the gateway (limits, dashboard, status endpoint) goes through this module so
that cache-creation tokens are counted once and weighted tokens and cost come from one price table.

`cache_creation_input_tokens` is the total; `cache_creation.ephemeral_5m/1h` is its breakdown when
present. Rows with no breakdown are priced as 5-minute writes.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .config import Pricing

CW5 = "(CASE WHEN cache_creation_5m + cache_creation_1h > 0 THEN cache_creation_5m ELSE cache_creation_tokens END)"
CW1 = "(CASE WHEN cache_creation_5m + cache_creation_1h > 0 THEN cache_creation_1h ELSE 0 END)"

# Only requests that reached an upstream count toward usage (spec 8: rejections never count).
FORWARDED = "rejected_by IS NULL"


def raw_tokens_sql() -> str:
    return f"(input_tokens + output_tokens + cache_read_tokens + {CW5} + {CW1})"


@dataclass
class Totals:
    requests: int = 0
    input: int = 0
    output: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    cost_usd: float = 0.0
    weighted: float = 0.0
    unpriced_models: set = field(default_factory=set)

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def raw(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    @property
    def cache_hit_ratio(self) -> float | None:
        denom = self.input + self.cache_write + self.cache_read
        return self.cache_read / denom if denom else None

    def add(self, other: "Totals") -> None:
        for f in ("requests", "input", "output", "cache_write_5m", "cache_write_1h", "cache_read", "cost_usd", "weighted"):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        self.unpriced_models |= other.unpriced_models

    def to_dict(self) -> dict:
        return {"requests": self.requests, "input": self.input, "output": self.output,
                "cache_write": self.cache_write, "cache_read": self.cache_read, "raw": self.raw,
                "weighted": round(self.weighted), "cost_usd": round(self.cost_usd, 4),
                "cache_hit_ratio": self.cache_hit_ratio, "unpriced_models": sorted(self.unpriced_models)}


def price_totals(pricing: Pricing, model: str | None, t: Totals) -> Totals:
    price = pricing.price_for(model)
    if price is None:
        if t.requests and model:
            t.unpriced_models.add(model)
        return t
    t.cost_usd = (t.input * price.input + t.output * price.output + t.cache_write_5m * price.cache_write_5m
                  + t.cache_write_1h * price.cache_write_1h + t.cache_read * price.cache_read) / 1e6
    t.weighted = t.cost_usd * 1e6 / pricing.reference_input()
    return t


SUMS = (f"COUNT(*) AS n, COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o, "
        f"COALESCE(SUM({CW5}),0) AS c5, COALESCE(SUM({CW1}),0) AS c1, COALESCE(SUM(cache_read_tokens),0) AS cr")


def grouped(conn: sqlite3.Connection, pricing: Pricing, where: str = "1=1", params: tuple = (),
            group: str | None = None) -> dict:
    """Totals keyed by the value of the SQL expression `group` (or `None` for a single total).

    Rows are always split by model first so each is priced with its own rate.
    """
    gexpr = group or "NULL"
    sql = (f"SELECT {gexpr} AS g, model, {SUMS} FROM requests "
           f"WHERE {FORWARDED} AND ({where}) GROUP BY g, model")
    out: dict = {}
    for r in conn.execute(sql, params):
        t = price_totals(pricing, r["model"], Totals(r["n"], r["i"], r["o"], r["c5"], r["c1"], r["cr"]))
        out.setdefault(r["g"], Totals()).add(t)
    return out


def total(conn: sqlite3.Connection, pricing: Pricing, where: str = "1=1", params: tuple = ()) -> Totals:
    return grouped(conn, pricing, where, params).get(None, Totals())


def series(conn: sqlite3.Connection, pricing: Pricing, since: float, bucket_s: int, split: str | None,
           where: str = "1=1", params: tuple = (), tz_offset_s: int = 0) -> list[dict]:
    """Time series in buckets of `bucket_s` seconds, optionally split by user, model or provider.

    `tz_offset_s` aligns day and week buckets to the viewer's local midnight.
    """
    split_expr = {"user": "user_id", "model": "model", "provider": "provider", None: "NULL"}[split]
    b, off = int(bucket_s), int(tz_offset_s)
    tb = f"(CAST((started_at + {off}) / {b} AS INTEGER) * {b} - {off})"
    sql = (f"SELECT {tb} AS t, {split_expr} AS s, model, {SUMS} FROM requests "
           f"WHERE {FORWARDED} AND started_at >= ? AND ({where}) GROUP BY t, s, model ORDER BY t")
    acc: dict[tuple, Totals] = {}
    for r in conn.execute(sql, (since, *params)):
        t = price_totals(pricing, r["model"], Totals(r["n"], r["i"], r["o"], r["c5"], r["c1"], r["cr"]))
        acc.setdefault((r["t"], r["s"]), Totals()).add(t)
    return [{"t": k[0], "key": k[1], **v.to_dict()} for k, v in sorted(acc.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))]
