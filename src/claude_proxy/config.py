from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class ListenerConfig:
    host: str = field(default_factory=lambda: _env("CLAUDE_PROXY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("CLAUDE_PROXY_PORT", "8080")))
    dashboard_port: int = field(default_factory=lambda: int(_env("CLAUDE_PROXY_DASHBOARD_PORT", "8081")))


@dataclass
class UpstreamConfig:
    base_url: str = field(default_factory=lambda: _env("CLAUDE_PROXY_UPSTREAM", "https://api.anthropic.com"))
    timeout_s: float = 300.0


@dataclass
class CredentialConfig:
    # OAuth-only per user request — api_key removed
    client_id: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e"))
    authorize_url: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_AUTHORIZE_URL", "https://claude.ai/oauth/authorize"))
    token_url: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_TOKEN_URL", "https://platform.claude.com/v1/oauth/token"))
    scopes: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_SCOPES", "user:inference user:profile user:sessions:claude_code user:mcp_servers user:file_upload"))
    beta_flag: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_BETA", "oauth-2025-04-20"))
    usage_url: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_USAGE_URL", "https://api.anthropic.com/api/oauth/usage"))
    redirect_uri: str = field(default_factory=lambda: _env("CLAUDE_PROXY_OAUTH_REDIRECT_URI", "https://platform.claude.com/oauth/code/callback"))


@dataclass
class DBConfig:
    path: str = field(default_factory=lambda: _env("CLAUDE_PROXY_DB", "claude_proxy.db"))


@dataclass
class TokenWeights:
    output: float = 5.0
    cache_write_5m: float = 1.25
    cache_write_1h: float = 2.0
    cache_read: float = 0.1
    model_multipliers: dict[str, float] = field(default_factory=lambda: {"sonnet": 1.0, "opus": 3.0, "haiku": 0.33, "default": 1.0})


@dataclass
class Config:
    listener: ListenerConfig = field(default_factory=ListenerConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    credential: CredentialConfig = field(default_factory=CredentialConfig)
    db: DBConfig = field(default_factory=DBConfig)
    weights: TokenWeights = field(default_factory=TokenWeights)
    retention_days: int = 180
    config_file: str | None = None

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        cfg = cls()
        toml_path = path or os.environ.get("CLAUDE_PROXY_CONFIG")
        if toml_path and os.path.exists(toml_path):
            try:
                import tomllib

                with open(toml_path, "rb") as f:
                    data = tomllib.load(f)
                if "listener" in data:
                    for k, v in data["listener"].items():
                        if hasattr(cfg.listener, k):
                            setattr(cfg.listener, k, v)
                if "upstream" in data:
                    for k, v in data["upstream"].items():
                        if hasattr(cfg.upstream, k):
                            setattr(cfg.upstream, k, v)
                if "credential" in data:
                    for k, v in data["credential"].items():
                        if hasattr(cfg.credential, k):
                            setattr(cfg.credential, k, v)
                if "db" in data and "path" in data["db"]:
                    cfg.db.path = data["db"]["path"]
                if "retention_days" in data:
                    cfg.retention_days = int(data["retention_days"])
                cfg.config_file = toml_path
            except Exception:
                pass
        return cfg
