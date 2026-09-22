from __future__ import annotations

import httpx
from fastapi import Request, Response

# Hop-by-hop headers per RFC 9110 §7.6.1 must not be forwarded
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "proxy-connection",
}


def should_forward(path: str) -> bool:
    # Only /v1/ is forwarded (spec 4, guards teamclaude #420)
    return path.startswith("/v1/")


def filter_request_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    # Also handle Connection header tokens
    connection_tokens = set()
    if "connection" in {k.lower() for k in headers}:
        for k, v in headers.items():
            if k.lower() == "connection":
                for tok in v.split(","):
                    connection_tokens.add(tok.strip().lower())
    for k, v in headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP or lk in connection_tokens:
            continue
        # Strip client credential headers — will be replaced by backend (spec 4.4)
        if lk in ("authorization", "x-api-key"):
            continue
        out[k] = v
    return out


def filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP:
            continue
        # httpx auto-decompresses upstream gzip (stream=True still decodes), so forwarding
        # content-encoding/content-length would cause client ZlibError / length mismatch.
        # Strip them and let StreamingResponse use chunked encoding (spec 4.5).
        if lk in ("content-encoding", "content-length"):
            continue
        out[k] = v
    return out
