from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("claude_proxy")

MAX_LINE_BYTES = 1_048_576  # 1 MiB per spec 6.3


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


def _safe_int(v, default=0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def _apply_usage_to_result(res: MeterResult, usage: dict, overwrite_only_present: bool = False):
    """Apply usage dict fields to result. If overwrite_only_present, only overwrite when key exists.
    Used for message_start (initial) and message_delta (overwrite)."""
    if not isinstance(usage, dict):
        return
    # direct fields
    if "input_tokens" in usage:
        res.input_tokens = _safe_int(usage["input_tokens"])
    elif not overwrite_only_present and "input_tokens" not in usage:
        pass

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
        # Some payloads use cache_creation key directly at top-level already handled; fallback
    # Some payloads put ephemeral fields at top level directly (defensive)
    if "ephemeral_5m_input_tokens" in usage:
        res.cache_creation_5m = _safe_int(usage["ephemeral_5m_input_tokens"])
    if "ephemeral_1h_input_tokens" in usage:
        res.cache_creation_1h = _safe_int(usage["ephemeral_1h_input_tokens"])


class SSEMeter:
    """Bounded SSE parser per spec 6.1-6.3.

    - Buffers at most one event (processes as soon as \\n\\n found)
    - Caps line buffer at 1 MiB; on cap sets meter_error and stops metering but forwarding continues
    - SSE rule: message_start usage -> input/cache/model/id, message_delta overwrites output (cumulative) and overwrites input/cache if present
    - Missing message_stop -> complete=false
    """

    def __init__(self):
        self._buf: str = ""
        self.result = MeterResult(stream=True, complete=False)
        self._seen_stop = False

    @property
    def meter_error(self) -> bool:
        return self.result.meter_error

    def feed(self, chunk: bytes):
        if self.result.meter_error:
            return
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        self._buf += text

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
            _apply_usage_to_result(self.result, usage, overwrite_only_present=False)

        elif typ == "message_delta":
            usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
            # Some deltas carry usage inside delta
            if not usage and isinstance(data.get("delta"), dict) and isinstance(data["delta"].get("usage"), dict):
                usage = data["delta"]["usage"]
            # Per spec: overwrites output_tokens (cumulative, never sum), overwrites input/cache if present
            _apply_usage_to_result(self.result, usage, overwrite_only_present=True)
            # Also model may appear in delta? ignore

        elif typ == "message_stop":
            self.result.complete = True
            self._seen_stop = True

        elif typ == "content_block_delta" or typ == "content_block_start" or typ == "content_block_stop":
            # no usage
            pass
        elif typ == "error":
            # record but not complete
            pass
        else:
            # Unknown event — try fallback: if data contains usage directly (defensive)
            if isinstance(data, dict) and "usage" in data:
                _apply_usage_to_result(self.result, data["usage"], overwrite_only_present=True)

    def finalize(self) -> MeterResult:
        # If we never saw message_stop, complete remains False per spec
        # If meter_error, we keep counts seen so far
        return self.result


def parse_non_streaming(body: bytes, path: str) -> MeterResult:
    """Parse non-streaming JSON response per spec 6.2."""
    res = MeterResult(stream=False, complete=True)
    # count_tokens endpoint -> zero tokens
    if "count_tokens" in path:
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
    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, dict):
        _apply_usage_to_result(res, usage, overwrite_only_present=False)
    if isinstance(data, dict):
        if isinstance(data.get("model"), str):
            res.model = data["model"]
        if isinstance(data.get("id"), str):
            res.upstream_request_id = data["id"]
        # Some non-streaming may still have error type
    return res


def weighted_tokens(result: MeterResult, weights) -> float:
    """Compute weighted tokens per spec 6.4, applied on read."""
    # weights is TokenWeights from config
    base = float(result.input_tokens) * 1.0
    base += float(result.output_tokens) * float(getattr(weights, "output", 5.0))
    # cache creation: prefer granular 5m/1h, fallback to generic
    c5 = float(result.cache_creation_5m)
    c1 = float(result.cache_creation_1h)
    cg = float(result.cache_creation_tokens)
    cw5 = float(getattr(weights, "cache_write_5m", 1.25))
    cw1 = float(getattr(weights, "cache_write_1h", 2.0))
    if c5 or c1:
        base += c5 * cw5
        base += c1 * cw1
        # if generic present and granular zero, it would be double counted — handle only if both zero we already covered
        # If generic non-zero but granular present, ignore generic to avoid double count
    else:
        base += cg * cw5
    base += float(result.cache_read_tokens) * float(getattr(weights, "cache_read", 0.1))

    # model multiplier
    mult = 1.0
    try:
        m = (result.model or "").lower()
        mm = getattr(weights, "model_multipliers", {})
        if isinstance(mm, dict):
            found = False
            for k, v in mm.items():
                if k == "default":
                    continue
                if k.lower() in m:
                    mult = float(v)
                    found = True
                    break
            if not found:
                mult = float(mm.get("default", 1.0))
    except Exception:
        mult = 1.0
    return base * mult


def weighted_from_row(row: dict | MeterResult, weights) -> float:
    """Helper for DB rows (sqlite Row dict)."""
    if isinstance(row, MeterResult):
        return weighted_tokens(row, weights)
    # row is dict-like with token columns
    mr = MeterResult(
        input_tokens=int(row.get("input_tokens") or 0),
        output_tokens=int(row.get("output_tokens") or 0),
        cache_creation_tokens=int(row.get("cache_creation_tokens") or 0),
        cache_creation_5m=int(row.get("cache_creation_5m") or 0),
        cache_creation_1h=int(row.get("cache_creation_1h") or 0),
        cache_read_tokens=int(row.get("cache_read_tokens") or 0),
        model=row.get("model"),
    )
    return weighted_tokens(mr, weights)
