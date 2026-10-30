"""Archive URL normalisation shared by every layer (stdlib only).

Kept in core so that core helpers never depend on platform parsers.
"""
import html
import re


def original_url(value: str) -> str:
    value = html.unescape(str(value or "")).replace("\\/", "/").replace("\\u0026", "&")
    if value.startswith("//"):
        value = "https:" + value
    m = re.match(r"^(?:https?://web\.archive\.org)?/web/\d{1,14}[a-z_]*?/(https?://.+)$", value)
    return m.group(1) if m else value
