from __future__ import annotations

import re

# Hop-by-hop headers (RFC 9110 section 7.6.1) are never forwarded.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "trailers", "transfer-encoding", "upgrade", "proxy-connection"}

# Client credentials: removed on every route before the route's own credential is added (spec 17.1).
CLIENT_CREDENTIALS = {"authorization", "x-api-key", "x-gateway-key"}

# Recomputed by the HTTP client for the outgoing request.
RECOMPUTED = {"host", "content-length"}

# Added by a reverse proxy or CDN in front of the gateway (Cloudflare, nginx). Forwarding them would
# leak each user's IP to the provider and make requests look unlike Claude Code's.
REVERSE_PROXY = {"cdn-loop", "true-client-ip", "x-real-ip", "forwarded", "via"}
REVERSE_PROXY_PREFIXES = ("cf-", "x-forwarded-")

SESSION_HEADERS = ("x-claude-code-session-id", "x-session-id", "anthropic-session-id")

COUNT_TOKENS_PATH = "/v1/messages/count_tokens"

# Plain segments only: no `.`/`..` (the HTTP client collapses them, so `/v1/../api` would leave /v1/ and
# `/v1/messages/count_tokens/../../messages` would dodge limits), no empty segments and no `%`, which an
# upstream proxy might decode into either.
_FORWARDABLE = re.compile(r"/v1(?:/[A-Za-z0-9_\-~.]+)+")


def should_forward(path: str) -> bool:
    # Only /v1/ is forwarded, so a mistyped path is never sent upstream with the credential.
    return bool(_FORWARDABLE.fullmatch(path)) and not any(seg in (".", "..") for seg in path.split("/"))


def _connection_tokens(headers: dict[str, str]) -> set[str]:
    return {t.strip().lower() for k, v in headers.items() if k.lower() == "connection" for t in v.split(",")}


def filter_request_headers(headers: dict[str, str]) -> dict[str, str]:
    drop = HOP_BY_HOP | CLIENT_CREDENTIALS | RECOMPUTED | REVERSE_PROXY | _connection_tokens(headers)
    return {k: v for k, v in headers.items()
            if k.lower() not in drop and not k.lower().startswith(REVERSE_PROXY_PREFIXES)}


def filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    # httpx decodes compressed bodies, so content-encoding and content-length no longer describe
    # what we send; the response is re-framed with chunked encoding.
    drop = HOP_BY_HOP | {"content-encoding", "content-length"} | _connection_tokens(headers)
    return {k: v for k, v in headers.items() if k.lower() not in drop}


# What the subscription tells about itself in responses: account quota, reset times, the organization.
# Only admins get these; to anyone else the gateway is all there is.
ACCOUNT_HEADER_PREFIXES = ("anthropic-ratelimit-", "anthropic-organization")


def strip_account_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if not k.lower().startswith(ACCOUNT_HEADER_PREFIXES)}


def merge_beta(existing: str | None, flag: str) -> str:
    flags = [f.strip() for f in (existing or "").split(",") if f.strip()]
    if flag not in flags:
        flags.append(flag)
    return ", ".join(flags)


def session_id(headers) -> str | None:
    for name in SESSION_HEADERS:
        if headers.get(name):
            return headers[name][:128]
    return None


# The subscription credential serves only requests whose system prompt opens with Claude Code's identity line as a
# block of its own; anything else, the same line with more text after it included, gets a bare 429 "Error" with no
# rate-limit headers (seen 2026-10-25 from curl and SDK clients).
CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."


def with_claude_code_identity(data: dict) -> bool:
    """Prepend the identity line to `data["system"]` unless its first block is exactly that. True if `data` changed."""
    system = data.get("system")
    first = system if isinstance(system, str) else system[0].get("text") if isinstance(system, list) and system \
        and isinstance(system[0], dict) else None
    if first == CLAUDE_CODE_IDENTITY:
        return False
    rest = [] if not system else [{"type": "text", "text": system}] if isinstance(system, str) else system
    if not isinstance(rest, list):
        return False   # malformed; let Anthropic reject it
    data["system"] = [{"type": "text", "text": CLAUDE_CODE_IDENTITY}, *rest]
    return True
