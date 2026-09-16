"""Shared presentation layer for every PLEIADES frontend.

Colour values, token formatting and tool-row formatting live here so the
Textual app and the legacy rich TUI cannot drift apart. Nothing in this module
imports a UI framework at import time except the small rich `Theme` builder,
which is only used by the legacy frontend.

The palette is pi's dark theme, copied verbatim from
``packages/coding-agent/src/modes/interactive/theme/dark.json``.
"""
from __future__ import annotations

import json

PREVIEW_MAX = 88    # tool result preview — with its 4-space indent this still
                    # fits one line on a 100-column terminal
ARGS_MAX = 80       # tool call args summary
TOOL_ROWS_MAX = 8   # tool blocks shown before collapsing into "… +N earlier"

PI = {
    "text": "#d4d4d4",
    "muted": "#808080",
    "dim": "#666666",
    "accent": "#8abeb7",
    "success": "#b5bd68",
    "error": "#cc6666",
    "warning": "#ffff00",
    "borderMuted": "#505050",
    "userMessageBg": "#343541",
    "toolPendingBg": "#282832",
    "toolSuccessBg": "#283228",
    "toolErrorBg": "#3c2828",
    "thinkingText": "#808080",
    "toolOutput": "#808080",
    "mdHeading": "#f0c674",
    "mdCode": "#8abeb7",
    "mdCodeBlock": "#b5bd68",
    "mdLink": "#81a2be",
}

#: Markdown element -> style string. Keys are rich's ``markdown.*`` style names;
#: the Textual frontend reuses the *colours* (see ``MD_COLOR``) in its CSS.
MD_STYLE = {
    "markdown.h1": f"bold {PI['mdHeading']}",
    "markdown.h2": f"bold {PI['mdHeading']}",
    "markdown.h3": f"bold {PI['mdHeading']}",
    "markdown.h4": PI["mdHeading"],
    "markdown.h5": PI["mdHeading"],
    "markdown.h6": PI["mdHeading"],
    "markdown.code": PI["mdCode"],
    "markdown.code_block": PI["mdCodeBlock"],
    "markdown.block_quote": PI["muted"],
    "markdown.item.bullet": PI["accent"],
    "markdown.item.number": PI["accent"],
    "markdown.link": PI["mdLink"],
    "markdown.link_url": PI["dim"],
    "markdown.hr": PI["muted"],
    "markdown.strong": f"bold {PI['text']}",
    "markdown.em": f"italic {PI['text']}",
    "markdown.table.header": f"bold {PI['text']}",
    "markdown.table.border": PI["borderMuted"],
}

#: Plain colour per markdown element, for frameworks that take colours not styles.
MD_COLOR = {
    "heading": PI["mdHeading"],
    "code": PI["mdCode"],
    "code_block": PI["mdCodeBlock"],
    "quote": PI["muted"],
    "bullet": PI["accent"],
    "link": PI["mdLink"],
    "link_url": PI["dim"],
    "hr": PI["muted"],
    "strong": PI["text"],
    "em": PI["text"],
    "table_header": PI["text"],
    "table_border": PI["borderMuted"],
}

#: Tool lifecycle state -> band background and (glyph, colour).
TOOL_STATE = {
    "run": {"bg": PI["toolPendingBg"], "glyph": "●", "color": PI["muted"]},
    "ok": {"bg": PI["toolSuccessBg"], "glyph": "✓", "color": PI["success"]},
    "err": {"bg": PI["toolErrorBg"], "glyph": "✗", "color": PI["error"]},
}

THINKING_STYLE = f"italic {PI['thinkingText']}"


def rich_theme():
    """Build rich's `Theme` from :data:`MD_STYLE` (legacy TUI only)."""
    from rich.theme import Theme
    return Theme(MD_STYLE)


def thinking_label(provider) -> str:
    """The active thinking level, as pi shows it next to the model name."""
    try:
        params = provider.thinking_params()
    except Exception:
        return ""
    if "reasoning_effort" in params:
        effort = str(params["reasoning_effort"])
        return "off" if effort in ("none", "off") else effort
    reasoning = params.get("reasoning")
    if isinstance(reasoning, dict):
        # OpenRouter dialect: {"reasoning": {"effort": ...}} or {"enabled": False}
        if reasoning.get("enabled") is False:
            return "off"
        effort = reasoning.get("effort")
        return str(effort) if effort else "on"
    return "on" if params.get("enable_thinking") else "off"


def format_tokens(count: int) -> str:
    """Compact token counts for the footer (pi prints 1.0k / 13.9k / 1.2M)."""
    count = int(count or 0)
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    if count < 1_000_000:
        return f"{round(count / 1000)}k"
    if count < 10_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{round(count / 1_000_000)}M"


def preview(text: str, limit: int = PREVIEW_MAX) -> str:
    """One-line, length-capped tool output preview."""
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def args_summary(args) -> str:
    """Compact one-line summary of a tool call's arguments."""
    if args is None:
        return ""
    try:
        return preview(json.dumps(args, ensure_ascii=False), ARGS_MAX)
    except Exception:
        return str(args)[:ARGS_MAX]
