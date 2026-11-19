"""What is left of the credit API (2026-10-21 spec) since API keys became pass app keys and rates went (2026-11-19
spec): `refuse_extras`, which refuses features priced above plain tokens on app keys."""
from __future__ import annotations

from .limits import Decision

REJECT_EXTRA = "api_extra"


def _err(kind: str, message: str) -> dict:
    return {"type": "error", "error": {"type": kind, "message": message}}


# ---------- app keys ----------

def refuse_extras(data: dict | None, beta_header: str | None, app: bool = False) -> Decision | None:
    """Features Anthropic prices above plain tokens: refused on pass app keys (`app`). Without `app` the wording sends
    the user to Claude Code's switches."""
    tools = data.get("tools") if isinstance(data, dict) else None
    betas = [b.strip() for b in (beta_header or "").split(",")]
    msg = None
    if isinstance(tools, list) and any(isinstance(t, dict) and str(t.get("type", "")).startswith("web_search") for t in tools):
        msg = ("Web search is not available with pass app keys; turn web search off in the app." if app else
               "Web search is not available with API keys; turn off the WebSearch tool.")
    elif (isinstance(data, dict) and data.get("speed") not in (None, "standard")) or any(b.startswith("fast-mode") for b in betas):
        msg = "Fast mode is not available with pass app keys." if app else "Fast mode is not available with API keys; turn it off with /fast."
    elif any(b.startswith("context-1m") for b in betas):
        msg = ("The 1M-token context is not available with pass app keys; pick a standard model." if app else
               "The 1M-token context is not available with API keys; pick a model without [1m].")
    return Decision(400, REJECT_EXTRA, _err("invalid_request_error", msg)) if msg else None
