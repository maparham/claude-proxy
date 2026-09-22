from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


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
class DBConfig:
    path: str = field(default_factory=lambda: _env("CLAUDE_PROXY_DB", "claude_proxy.db"))


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
    )]


@dataclass
class Config:
    listener: ListenerConfig = field(default_factory=ListenerConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    credential: CredentialConfig = field(default_factory=CredentialConfig)
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    db: DBConfig = field(default_factory=DBConfig)
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
                    "quota": cfg.quota, "db": cfg.db}
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
        cfg.config_file = toml_path
        return cfg
