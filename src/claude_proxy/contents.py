"""The full request and reply of API clients' requests (SDKs, curl: an API key, no session id), kept so the key's
owner and the admin can read them in the Requests tab. Claude Code's own traffic is never kept, only its titles."""
from __future__ import annotations

import json
import sqlite3
import zlib

MAX_REPLY_BYTES = 8 * 1024 * 1024   # a reply is kept up to this; the rest is cut and the row says so


def wanted(is_api: bool, session_id: str | None, method: str, path: str) -> bool:
    return is_api and not session_id and method == "POST" and path == "/v1/messages"


class Capture:
    """The reply's bytes as they pass through the relay, up to MAX_REPLY_BYTES."""

    def __init__(self):
        self.buf = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        room = MAX_REPLY_BYTES - len(self.buf)
        if len(chunk) > room:
            self.truncated = True
        self.buf.extend(chunk[:max(room, 0)])


def save(conn: sqlite3.Connection, request_id: int, request: bytes, reply: bytes, stream: bool, truncated: bool) -> None:
    conn.execute("INSERT INTO request_contents(request_id, request, reply, stream, truncated) VALUES(?,?,?,?,?)",
                 (request_id, zlib.compress(request), zlib.compress(reply), int(stream), int(truncated)))


def _json_or_text(raw: bytes):
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def assemble(raw: bytes) -> dict | None:
    """A streamed reply put back together as the message a non-streaming request would have got, or None when
    the stream holds no message_start (an error event, say)."""
    msg, blocks, partial = None, {}, {}
    for event in raw.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n\n"):
        data = "".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        try:
            e = json.loads(data) if data else None
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        kind, i = e.get("type"), e.get("index")
        if kind == "message_start" and isinstance(e.get("message"), dict):
            msg = e["message"]
        elif kind == "content_block_start" and isinstance(e.get("content_block"), dict):
            blocks[i] = dict(e["content_block"])
        elif kind == "content_block_delta" and i in blocks and isinstance(e.get("delta"), dict):
            d, b = e["delta"], blocks[i]
            if d.get("type") == "input_json_delta":
                partial[i] = partial.get(i, "") + (d.get("partial_json") or "")
            for k in ("text", "thinking", "signature"):
                if isinstance(d.get(k), str):
                    b[k] = b.get(k, "") + d[k]
            if isinstance(d.get("citation"), dict):
                b.setdefault("citations", []).append(d["citation"])
        elif kind == "message_delta" and msg is not None:
            msg.update(e.get("delta") or {})
            msg["usage"] = {**(msg.get("usage") or {}), **(e.get("usage") or {})}
        elif kind == "error" and msg is None:
            return None
    if msg is None:
        return None
    for i, s in partial.items():
        try:
            blocks[i]["input"] = json.loads(s) if s else {}
        except ValueError:
            blocks[i]["input"] = s
    msg["content"] = [blocks[i] for i in sorted(blocks, key=lambda k: (k is None, k or 0))]
    return msg


def load(conn: sqlite3.Connection, request_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM request_contents WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        return None
    request, reply = zlib.decompress(row["request"]), zlib.decompress(row["reply"])
    out = {"request": _json_or_text(request), "stream": bool(row["stream"]), "truncated": bool(row["truncated"])}
    message = assemble(reply) if row["stream"] else None
    out["reply"] = message if message is not None else _json_or_text(reply)
    return out
