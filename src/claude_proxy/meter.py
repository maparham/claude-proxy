from __future__ import annotations

import codecs
import json
import logging
import math
from dataclasses import dataclass

from .forwarder import COUNT_TOKENS_PATH

logger = logging.getLogger("claude_proxy")

MAX_LINE_BYTES = 1_048_576  # 1 MiB per spec 6.3
MAX_TEXT_CHARS = 4096       # answer text kept when collect_text is on
CHARS_PER_TOKEN = 3.5       # for estimating the output of a stream cut short


@dataclass
class MeterResult:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_creation_5m: int = 0
    cache_creation_1h: int = 0
    cache_read_tokens: int = 0
    model: str | None = None
    upstream_request_id: str | None = None
    complete: bool = False
    meter_error: bool = False
    meter_error_detail: str | None = None
    stream: bool = False
    error_type: str | None = None
    text: str = ""   # answer text, kept only when asked for (session titles)


def _safe_int(v, default=0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def _apply_usage_to_result(res: MeterResult, usage: dict | None):
    """Overwrite result fields with every usage field present (message_start, message_delta, JSON body).

    Counts in message_delta are cumulative, so they replace earlier values and are never summed.
    """
    if not isinstance(usage, dict):
        return
    if "input_tokens" in usage:
        res.input_tokens = _safe_int(usage["input_tokens"])

    if "output_tokens" in usage:
        res.output_tokens = _safe_int(usage["output_tokens"])

    if "cache_creation_input_tokens" in usage:
        res.cache_creation_tokens = _safe_int(usage["cache_creation_input_tokens"])

    if "cache_read_input_tokens" in usage:
        res.cache_read_tokens = _safe_int(usage["cache_read_input_tokens"])

    # nested cache_creation ephemeral
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        if "ephemeral_5m_input_tokens" in cc:
            res.cache_creation_5m = _safe_int(cc["ephemeral_5m_input_tokens"])
        if "ephemeral_1h_input_tokens" in cc:
            res.cache_creation_1h = _safe_int(cc["ephemeral_1h_input_tokens"])


class SSEMeter:
    """Bounded SSE parser per spec 6.1-6.3.

    - Buffers at most one event (processes as soon as \\n\\n found)
    - Caps line buffer at 1 MiB; on cap sets meter_error and stops metering but forwarding continues
    - SSE rule: message_start usage -> input/cache/model/id, message_delta overwrites output (cumulative) and overwrites input/cache if present
    - Missing message_stop -> complete=false
    """

    def __init__(self, collect_text: bool = False):
        self._buf: str = ""
        self.result = MeterResult(stream=True, complete=False)
        self._seen_stop = False
        self._collect_text = collect_text
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")   # chunks may split a character
        self._streamed_chars = 0

    @property
    def meter_error(self) -> bool:
        return self.result.meter_error

    def feed(self, chunk: bytes):
        if self.result.meter_error:
            return
        self._buf += self._decoder.decode(chunk)

        # Quick cap check: if buffer grows huge without delimiter
        if len(self._buf.encode("utf-8")) > MAX_LINE_BYTES and "\n\n" not in self._buf and "\r\n\r\n" not in self._buf:
            # Check if any single line already exceeds
            # Simplest: if buffer exceeds cap, flag error and stop metering
            self.result.meter_error = True
            self.result.meter_error_detail = "line_cap"
            logger.warning("meter line cap hit (%d bytes) — metering stopped", len(self._buf.encode("utf-8")))
            self._buf = ""
            return

        # Process at most one event per feed iteration? Spec says ≤1 event buffered — drain all available
        while True:
            idx = self._buf.find("\n\n")
            idx2 = self._buf.find("\r\n\r\n")
            if idx == -1 and idx2 == -1:
                # No complete event yet; check if current partial line already over cap
                if len(self._buf.encode("utf-8")) > MAX_LINE_BYTES:
                    self.result.meter_error = True
                    self.result.meter_error_detail = "line_cap"
                    logger.warning("meter line cap hit on partial event — metering stopped")
                    self._buf = ""
                break
            # choose earliest delimiter
            if idx != -1 and idx2 != -1:
                if idx < idx2:
                    use_idx, delim_len = idx, 2
                else:
                    use_idx, delim_len = idx2, 4
            elif idx != -1:
                use_idx, delim_len = idx, 2
            else:
                use_idx, delim_len = idx2, 4

            block = self._buf[:use_idx]
            self._buf = self._buf[use_idx + delim_len :]

            if len(block.encode("utf-8")) > MAX_LINE_BYTES:
                self.result.meter_error = True
                self.result.meter_error_detail = "event_cap"
                logger.warning("meter event cap hit (%d bytes) — metering stopped", len(block.encode("utf-8")))
                break

            if not block.strip():
                continue

            try:
                self._process_event_block(block)
            except Exception as e:
                logger.warning("meter process block failed: %s", e)
                self.result.meter_error = True
                self.result.meter_error_detail = f"parse_error:{e}"
                break

            if self.result.meter_error:
                break
            # continue to drain if multiple events arrived in one chunk

    def _process_event_block(self, block: str):
        # Extract event: and data: lines per SSE spec
        event_type: str | None = None
        data_lines: list[str] = []
        for raw_line in block.splitlines():
            # SSE lines may have \r
            line = raw_line.rstrip("\r")
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                # data: may be "data: {json}" or "data:"
                data_lines.append(line[len("data:"):].lstrip())
            elif line.startswith("data"):
                # handle "data" without colon (unlikely)
                if line == "data":
                    data_lines.append("")
        if not data_lines:
            return
        data_str = "\n".join(data_lines)
        if not data_str.strip():
            return
        try:
            data = json.loads(data_str)
        except Exception as e:
            self.result.meter_error = True
            self.result.meter_error_detail = f"json_error:{e}"
            logger.warning("meter json error: %s data=%.200s", e, data_str)
            return

        # Anthropic SSE: either event_type field or data.type
        typ = event_type or data.get("type")

        if typ == "message_start":
            msg = data.get("message", {})
            if not isinstance(msg, dict):
                msg = {}
            usage = msg.get("usage", {}) if isinstance(msg.get("usage"), dict) else {}
            model = msg.get("model")
            mid = msg.get("id")
            if isinstance(model, str):
                self.result.model = model
            if isinstance(mid, str):
                self.result.upstream_request_id = mid
            _apply_usage_to_result(self.result, usage)

        elif typ == "message_delta":
            usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
            # Some deltas carry usage inside delta
            if not usage and isinstance(data.get("delta"), dict) and isinstance(data["delta"].get("usage"), dict):
                usage = data["delta"]["usage"]
            # Per spec: overwrites output_tokens (cumulative, never sum), overwrites input/cache if present
            _apply_usage_to_result(self.result, usage)
            # Also model may appear in delta? ignore

        elif typ == "message_stop":
            self.result.complete = True
            self._seen_stop = True

        elif typ == "content_block_delta":
            delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
            for k in ("text", "thinking", "partial_json"):
                if isinstance(delta.get(k), str):
                    self._streamed_chars += len(delta[k])
            if self._collect_text and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                self.result.text = (self.result.text + delta["text"])[:MAX_TEXT_CHARS]
        elif typ == "content_block_start" or typ == "content_block_stop":
            # no usage
            pass
        elif typ == "error":
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            self.result.error_type = str(err.get("type") or "stream_error")[:64]

    def finalize(self) -> MeterResult:
        # If we never saw message_stop, complete remains False per spec
        # If meter_error, we keep counts seen so far
        if not self.result.complete and self._streamed_chars:
            # The output count arrives only at the end, so a stream cut short (the user pressed Esc, the
            # connection dropped) would otherwise record message_start's placeholder of ~1 token.
            estimate = math.ceil(self._streamed_chars / CHARS_PER_TOKEN)
            self.result.output_tokens = max(self.result.output_tokens, estimate)
        return self.result


def parse_non_streaming(body: bytes, path: str, collect_text: bool = False) -> MeterResult:
    """Parse non-streaming JSON response per spec 6.2."""
    res = MeterResult(stream=False, complete=True)
    # count_tokens endpoint -> zero tokens
    if path == COUNT_TOKENS_PATH:
        res.complete = True
        return res
    if not body:
        return res
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as e:
        res.meter_error = True
        res.meter_error_detail = f"json_error:{e}"
        return res
    if not isinstance(data, dict):
        return res
    _apply_usage_to_result(res, data.get("usage"))
    if isinstance(data.get("model"), str):
        res.model = data["model"]
    if isinstance(data.get("id"), str):
        res.upstream_request_id = data["id"]
    if data.get("type") == "error" and isinstance(data.get("error"), dict):
        res.error_type = str(data["error"].get("type") or "error")[:64]
    if collect_text and isinstance(data.get("content"), list):
        res.text = "".join(b["text"] for b in data["content"]
                           if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))[:MAX_TEXT_CHARS]
    return res
