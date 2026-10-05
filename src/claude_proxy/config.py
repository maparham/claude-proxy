from __future__ import annotations

import fnmatch
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field, fields

logger = logging.getLogger("claude_proxy")

def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def gateway_home() -> str | None:
    """`~/.claude-gateway` if it exists: the default home of the database and the credential key."""
    path = os.path.join(os.path.expanduser("~"), ".claude-gateway")
    return path if os.path.isdir(path) else None


def _default_db() -> str:
    if os.environ.get("CLAUDE_PROXY_DB"):
        return os.environ["CLAUDE_PROXY_DB"]
    home = gateway_home()
    return os.path.join(home, "claude_proxy.db") if home else "claude_proxy.db"


class ConfigError(Exception):
    pass


@dataclass
class ListenerConfig:
    host: str = field(default_factory=lambda: _env("CLAUDE_PROXY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("CLAUDE_PROXY_PORT", "8080")))
    dashboard_host: str = field(default_factory=lambda: _env("CLAUDE_PROXY_DASHBOARD_HOST", "127.0.0.1"))
    dashboard_port: int = field(default_factory=lambda: int(_env("CLAUDE_PROXY_DASHBOARD_PORT", "8081")))
    # Set when the dashboard is served over TLS (e.g. behind Caddy) so the session cookie is Secure.
    secure_cookies: bool = field(default_factory=lambda: _env("CLAUDE_PROXY_SECURE_COOKIES", "0") == "1")
    # Where people reach the two listeners (e.g. through a tunnel); needed for sign-up and `claude-gateway on`
    # authorizing in the browser: the install command, the authorize link and the origin Clerk tokens must come from.
    public_url: str = ""
    dashboard_url: str = ""
    # Optional: another host (e.g. the apex domain) that serves the public home page; its sign-in links and install
    # commands go on to dashboard_url, since Clerk's tokens are made for that origin only.
    home_url: str = ""


@dataclass
class UpstreamConfig:
    base_url: str = field(default_factory=lambda: _env("CLAUDE_PROXY_UPSTREAM", "https://api.anthropic.com"))
    timeout_s: float = 600.0


@dataclass
class CredentialConfig:
    client_id: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e"))
    # Values as used by Claude Code 2.1.280 for a claude.ai (subscription) login.
    authorize_url: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_AUTHORIZE_URL", "https://claude.com/cai/oauth/authorize"))
    token_url: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_TOKEN_URL", "https://platform.claude.com/v1/oauth/token"))
    scopes: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_SCOPES",
        "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload user:plugins"))
    refresh_scopes: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_REFRESH_SCOPES",
        "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload user:plugins"))
    beta_flag: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_BETA", "oauth-2025-04-20"))
    usage_url: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_USAGE_URL", "https://api.anthropic.com/api/oauth/usage"))
    redirect_uri: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_REDIRECT_URI", "https://platform.claude.com/oauth/code/callback"))
    # Sent only on the gateway's own calls (refresh, usage poll). Proxied requests keep the client's User-Agent.
    user_agent: str = field(default_factory=lambda: _env("CLAUDE_PROXY_USER_AGENT", "claude-code/2.1.280"))


@dataclass
class QuotaConfig:
    poll_idle_s: int = 600       # poll only when no header snapshot arrived for this long
    poll_min_interval_s: int = 180
    poll_max_backoff_s: int = 1800
    stale_after_s: int = 1800    # share limits are skipped when the newest snapshot is older


@dataclass
class SignupConfig:
    """Self-service accounts (sign-up design, 2026-09-26): people sign in with Clerk; a new account gets a one-time credit."""
    enabled: bool = False              # False: Clerk sign-in still links existing accounts, but creates none
    credit_usd: float = 5.0            # the new account's cost_total limit
    free_daily_cap_usd: float = 0.0    # what all accounts on a credit may spend together in any 24 h; 0: no cap
    clerk_publishable_key: str = ""    # public; the secret key is CLERK_SECRET_KEY in the environment
    installer_url: str = "https://raw.githubusercontent.com/maparham/claude-proxy/master/install.sh"
    installer_ps1_url: str = "https://raw.githubusercontent.com/maparham/claude-proxy/master/install.ps1"   # Windows

    def clerk_secret(self) -> str | None:
        return os.environ.get("CLERK_SECRET_KEY") or None


@dataclass
class LimitsConfig:
    # Requests one user may have in flight at once. Limits see only finished requests plus these, so a cost or token
    # limit can be overshot by at most this many requests.
    max_inflight: int = 8


LENGTHS = {"day": 1, "week": 7, "month": 30}   # ticket lengths in days (paid-tickets design, section 3)
ESTIMATE_DAYS = 30   # how far back the daily usage estimates read; retention must keep at least this much


@dataclass
class Tier:
    """A size of slice sold as tickets: its share of the account and its default USD prices per length."""
    label: str
    share_pct: float
    compare: str = ""
    default_usd: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.share_pct, bool) or not isinstance(self.share_pct, (int, float)) or not 0 < self.share_pct <= 100:
            raise TypeError("share_pct must be a number above 0 and at most 100")
        if set(self.default_usd) != set(LENGTHS):
            raise TypeError(f"default_usd needs exactly the keys {', '.join(LENGTHS)}")
        for k, v in self.default_usd.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                raise TypeError(f"default_usd.{k} must be a price above 0")


@dataclass
class Currency:
    round_to: float = 0.01

    def __post_init__(self):
        if isinstance(self.round_to, bool) or not isinstance(self.round_to, (int, float)) or self.round_to <= 0:
            raise TypeError("round_to must be a step above 0, e.g. 0.50")


def _default_tiers() -> dict[str, Tier]:
    return {
        "lite": Tier("Lite", 5, "Claude Pro", {"day": 3, "week": 8, "month": 20}),
        "standard": Tier("Standard", 25, "Claude Max 5x", {"day": 12, "week": 35, "month": 100}),
    }


@dataclass
class TicketsConfig:
    """Paid tickets (design 2026-10-03). `enabled` is set when the config file has a [tickets] section."""
    enabled: bool = False
    how_to_buy: str = ""
    max_sold_pct: float = 80      # ceiling on what tickets and bonuses may reserve; the rest is headroom
    tiers: dict[str, Tier] = field(default_factory=_default_tiers)
    currencies: dict[str, Currency] = field(default_factory=dict)   # USD is built in and must not appear here
    turnstile_site_key: str = ""  # public; with TURNSTILE_SECRET, visitors can order from the home page (order requests)

    def turnstile_secret(self) -> str | None:
        return os.environ.get("TURNSTILE_SECRET") or None

    def turnstile_on(self) -> bool:
        return bool(self.turnstile_site_key and self.turnstile_secret())


@dataclass
class EmailConfig:
    """[email] (order requests design, section 6): the SMTP server the order emails go out through."""
    smtp_host: str
    from_: str                    # `from` in the TOML file
    admin_to: str
    smtp_port: int = 587          # 587: STARTTLS; 465: implicit TLS
    smtp_user: str = ""           # empty: no AUTH

    def password(self) -> str | None:
        return os.environ.get("SMTP_PASSWORD") or None


@dataclass
class DBConfig:
    path: str = field(default_factory=_default_db)


@dataclass
class Price:
    """USD per million tokens."""
    input: float
    output: float
    cache_write_5m: float | None = None
    cache_write_1h: float | None = None
    cache_read: float | None = None

    def __post_init__(self):
        if self.cache_write_5m is None:
            self.cache_write_5m = self.input * 1.25
        if self.cache_write_1h is None:
            self.cache_write_1h = self.input * 2.0
        if self.cache_read is None:
            self.cache_read = self.input * 0.1


def _default_prices() -> dict[str, Price]:
    # Anthropic first-party API list prices (cached 2026-06-24) and Meta Model API prices.
    # Order matters: the first glob that matches the model wins.
    return {
        "claude-fable-5-1*": Price(10.0, 50.0, cache_read=0.25),
        "claude-mythos-5-1*": Price(10.0, 50.0, cache_read=0.25),
        "claude-fable-5*": Price(10.0, 50.0),
        "claude-opus-5-5*": Price(4.0, 20.0, cache_read=0.20),
        "claude-opus-5*": Price(5.0, 25.0),
        "claude-opus-4*": Price(5.0, 25.0),
        "claude-sonnet-5*": Price(2.0, 10.0),
        "claude-sonnet-4*": Price(3.0, 15.0),
        "claude-haiku-4*": Price(1.0, 5.0),
        "muse-spark-1.3-contributor*": Price(0.10, 0.20, cache_write_5m=0.10, cache_write_1h=0.10, cache_read=0.002),
        "muse-spark*": Price(1.25, 4.25, cache_write_5m=1.25, cache_write_1h=1.25, cache_read=0.15),
    }


@dataclass
class Pricing:
    prices: dict[str, Price] = field(default_factory=_default_prices)
    # Weighted tokens are API-equivalent cost expressed in units of this model's input tokens.
    reference_model: str = "claude-sonnet-5"

    def price_for(self, model: str | None) -> Price | None:
        m = (model or "").lower()
        for pattern, price in self.prices.items():
            if fnmatch.fnmatchcase(m, pattern.lower()):
                return price
        return None

    def reference_input(self) -> float:
        p = self.price_for(self.reference_model)
        return p.input if p else 1.0


@dataclass
class Route:
    """A third-party provider that speaks the Anthropic Messages wire format."""
    name: str
    base_url: str
    api_key_env: str
    models: list[str]
    model_map: dict[str, str] = field(default_factory=dict)
    strip_headers: list[str] = field(default_factory=lambda: ["anthropic-beta"])
    # Top-level request fields the provider rejects (e.g. Anthropic-only "context_management").
    drop_body_fields: list[str] = field(default_factory=list)
    auth_header: str = "authorization"  # "authorization" (Bearer) or "x-api-key"
    # Per listed model name: {"context": tokens, "output": tokens, "display_name": str}. Supplied by the admin;
    # /v1/models reports the sizes so OpenCode knows the real context window (spec 3.1).
    model_info: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self):
        for name, info in self.model_info.items():
            unknown = set(info) - {"context", "output", "display_name"}
            if unknown:
                raise TypeError(f"model_info[{name!r}]: unknown keys {sorted(unknown)}")
            if ("context" in info) != ("output" in info):
                raise TypeError(f"model_info[{name!r}]: give both context and output, or neither")
            for k in ("context", "output"):
                v = info.get(k)
                if k in info and (isinstance(v, bool) or not isinstance(v, int) or v <= 0):
                    raise TypeError(f"model_info[{name!r}].{k} must be a positive whole number of tokens")

    def listed_models(self) -> list[str]:
        """Globs can't be enumerated, so the models /v1/models lists are the model_map and model_info keys.
        A model that matches `models` but is in neither is still routed, just not listed."""
        return list(dict.fromkeys([*self.model_map, *self.model_info]))

    def matches(self, model: str | None) -> bool:
        return bool(model) and any(fnmatch.fnmatchcase(model, p) for p in self.models)

    def upstream_model(self, model: str) -> str:
        return self.model_map.get(model, model)

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None


def _default_routes() -> list[Route]:
    return [Route(
        name="meta",
        base_url="https://api.meta.ai",
        api_key_env="META_API_KEY",
        models=["muse-spark*"],
        model_map={"muse-spark": "muse-spark-1.3"},
        # 1M window as listed for muse-spark-1.3 by Promptfoo's Meta provider docs and OpenRouter; Meta publishes
        # no output limit for 1.3, so 32000 is a conservative cap.
        model_info={"muse-spark": {"context": 1048576, "output": 32000, "display_name": "Muse Spark 1.3"}},
    )]


_TIER_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")   # an ISO 4217 code, which the dashboard formats amounts with


def _load_tickets(toml_path: str, data: dict) -> TicketsConfig:
    t = TicketsConfig(enabled=True)
    data = dict(data)
    try:
        tiers = data.pop("tiers", None)
        if tiers is not None:
            for tier_id in tiers:
                if not _TIER_ID.match(tier_id):
                    raise ConfigError(f"{toml_path}: tier id {tier_id!r} must be lowercase letters, digits and underscores")
            t.tiers = {k: Tier(**v) for k, v in tiers.items()}
        t.currencies = {k.upper(): Currency(**v) for k, v in data.pop("currencies", {}).items()}
    except (TypeError, AttributeError) as e:   # AttributeError: a key that should be a table, e.g. `currencies = 5`
        raise ConfigError(f"{toml_path}: bad [tickets] entry: {e}") from e
    if "USD" in t.currencies:
        raise ConfigError(f"{toml_path}: USD is built in; do not configure it under [tickets.currencies]")
    for code in t.currencies:
        if not _CURRENCY.match(code):
            raise ConfigError(f"{toml_path}: currency {code!r} must be three letters, such as EUR")
    if "enabled" in data:
        raise ConfigError(f"{toml_path}: [tickets].enabled is not a setting: tickets are on while the [tickets] section exists; "
                          "remove the [tickets] section to switch them off")
    plain = [f.name for f in fields(TicketsConfig) if f.name not in ("enabled", "tiers", "currencies")]
    for k, v in data.items():
        if k not in plain:
            raise ConfigError(f"{toml_path}: unknown key [tickets].{k}; [tickets] takes {', '.join(plain)}, "
                              "[tickets.tiers.<id>] and [tickets.currencies.<code>]")
        setattr(t, k, v)
    if isinstance(t.max_sold_pct, bool) or not isinstance(t.max_sold_pct, (int, float)) or not 0 < t.max_sold_pct <= 100:
        raise ConfigError(f"{toml_path}: [tickets].max_sold_pct must be above 0 and at most 100")
    if not isinstance(t.turnstile_site_key, str):
        raise ConfigError(f"{toml_path}: [tickets].turnstile_site_key must be a string")
    if not t.tiers:
        raise ConfigError(f"{toml_path}: [tickets] needs at least one tier")
    for tid, tier in t.tiers.items():
        if tier.share_pct > t.max_sold_pct:   # it could never be sold, and nothing else would say why
            raise ConfigError(f"{toml_path}: [tickets.tiers.{tid}] share_pct {tier.share_pct:g} is above "
                              f"[tickets].max_sold_pct {t.max_sold_pct:g}, so it could never be sold")
    return t


def _load_email(toml_path: str, data: dict) -> EmailConfig:
    keys = {"smtp_host": str, "from": str, "admin_to": str, "smtp_port": int, "smtp_user": str}
    for k in data:
        if k not in keys:
            raise ConfigError(f"{toml_path}: unknown key [email].{k}; [email] takes {', '.join(keys)}")
    for k in ("smtp_host", "from", "admin_to"):
        if not data.get(k):
            raise ConfigError(f"{toml_path}: [email].{k} is required")
    for k, kind in keys.items():
        v = data.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, kind)):
            raise ConfigError(f"{toml_path}: [email].{k} must be a {'whole number' if kind is int else 'string'}")
    if "smtp_port" in data and not 0 < data["smtp_port"] < 65536:
        raise ConfigError(f"{toml_path}: [email].smtp_port must be a port number")
    e = EmailConfig(**{("from_" if k == "from" else k): v for k, v in data.items()})
    if e.smtp_user and not e.password():
        logger.warning("[email].smtp_user is set but SMTP_PASSWORD is not in the environment; every order email will fail")
    return e


@dataclass
class Config:
    listener: ListenerConfig = field(default_factory=ListenerConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    credential: CredentialConfig = field(default_factory=CredentialConfig)
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    db: DBConfig = field(default_factory=DBConfig)
    signup: SignupConfig = field(default_factory=SignupConfig)
    tickets: TicketsConfig = field(default_factory=TicketsConfig)
    email: EmailConfig | None = None      # None without an [email] section: no mail is sent
    pricing: Pricing = field(default_factory=Pricing)
    routes: list[Route] = field(default_factory=_default_routes)
    retention_days: int = 180
    config_file: str | None = None

    def route_for(self, model: str | None) -> Route | None:
        for r in self.routes:
            if r.matches(model):
                return r
        return None

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        cfg = cls()
        toml_path = path or os.environ.get("CLAUDE_PROXY_CONFIG")
        if not toml_path:
            return cfg
        if not os.path.exists(toml_path):
            raise ConfigError(f"config file not found: {toml_path}")
        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{toml_path}: {e}") from e

        sections = {"listener": cfg.listener, "upstream": cfg.upstream, "credential": cfg.credential,
                    "quota": cfg.quota, "limits": cfg.limits, "db": cfg.db, "signup": cfg.signup}
        for name, target in sections.items():
            for k, v in data.get(name, {}).items():
                if not hasattr(target, k):
                    raise ConfigError(f"{toml_path}: unknown key [{name}].{k}")
                setattr(target, k, v)
        if "retention_days" in data:
            cfg.retention_days = int(data["retention_days"])
        if "pricing" in data:
            p = data["pricing"]
            if "reference_model" in p:
                cfg.pricing.reference_model = p["reference_model"]
            for pattern, spec in p.get("models", {}).items():
                cfg.pricing.prices = {pattern: Price(**spec), **cfg.pricing.prices}
        if "routes" in data:
            try:
                cfg.routes = [Route(**r) for r in data["routes"]]
            except TypeError as e:
                raise ConfigError(f"{toml_path}: bad [[routes]] entry: {e}") from e
        if "tickets" in data:
            cfg.tickets = _load_tickets(toml_path, data["tickets"])
            if cfg.retention_days < ESTIMATE_DAYS:   # cleanup would delete what the 30-day usage estimates read
                raise ConfigError(f"{toml_path}: retention_days must be at least {ESTIMATE_DAYS} while [tickets] is on; "
                                  "the usage estimates read that many days")
            if cfg.tickets.turnstile_site_key and not cfg.tickets.turnstile_secret():
                logger.warning("[tickets] turnstile_site_key is set but TURNSTILE_SECRET is not in the environment; "
                               "visitors cannot order from the home page")
        if "email" in data:
            cfg.email = _load_email(toml_path, data["email"])
        cfg.config_file = toml_path
        return cfg
