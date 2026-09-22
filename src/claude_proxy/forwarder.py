from __future__ import annotations

# Hop-by-hop headers (RFC 9110 section 7.6.1) are never forwarded.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "trailers", "transfer-encoding", "upgrade", "proxy-connection"}

# Client credentials: removed on every route before the route's own credential is added (spec 17.1).
CLIENT_CREDENTIALS = {"authorization", "x-api-key"}

# Recomputed by the HTTP client for the outgoing request.
RECOMPUTED = {"host", "content-length"}

SESSION_HEADERS = ("x-claude-code-session-id", "x-session-id", "anthropic-session-id")


def should_forward(path: str) -> bool:
    # Only /v1/ is forwarded, so a mistyped path is never sent upstream with the credential.
    return path.startswith("/v1/")


def _connection_tokens(headers: dict[str, str]) -> set[str]:
    return {t.strip().lower() for k, v in headers.items() if k.lower() == "connection" for t in v.split(",")}


def filter_request_headers(headers: dict[str, str]) -> dict[str, str]:
    drop = HOP_BY_HOP | CLIENT_CREDENTIALS | RECOMPUTED | _connection_tokens(headers)
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    # httpx decodes compressed bodies, so content-encoding and content-length no longer describe
    # what we send; the response is re-framed with chunked encoding.
    drop = HOP_BY_HOP | {"content-encoding", "content-length"} | _connection_tokens(headers)
    return {k: v for k, v in headers.items() if k.lower() not in drop}


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
