"""Session titles: Claude Code names each session with a small side request whose answer is {"title": "..."}.

The gateway recognises that request and keeps the title, so the dashboard can show sessions by name.
It is the only piece of conversation content the gateway stores.
"""
from __future__ import annotations

import json

# Start of Claude Code's system prompt for the side request (2.1.x), used when the output schema changes shape.
PROMPT_PREFIX = "You are naming a coding session"
MAX_TITLE = 200


def _format(data: dict) -> dict | None:
    cfg = data.get("output_config")
    fmt = cfg.get("format") if isinstance(cfg, dict) else None
    fmt = fmt if isinstance(fmt, dict) else data.get("output_format")   # output_format is the deprecated spelling
    return fmt if isinstance(fmt, dict) else None


def _system_text(data: dict) -> str:
    system = data.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(b["text"] for b in system if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def is_title_request(data: dict | None) -> bool:
    """True for Claude Code's session-naming request: a JSON answer whose only field is `title`."""
    if not isinstance(data, dict):
        return False
    fmt = _format(data)
    if fmt and fmt.get("type") == "json_schema":
        props = (fmt.get("schema") or {}).get("properties") if isinstance(fmt.get("schema"), dict) else None
        if isinstance(props, dict) and set(props) == {"title"}:
            return True
    return PROMPT_PREFIX in _system_text(data)


def parse_title(text: str) -> str | None:
    """The title from the model's answer, or None when it is not the expected JSON."""
    text = text.strip()
    if text.startswith("```"):   # a model without structured outputs may fence its JSON
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        title = json.loads(text).get("title")
    except (ValueError, AttributeError):
        return None
    if not isinstance(title, str):
        return None
    title = " ".join(title.split())[:MAX_TITLE]
    return title or None
