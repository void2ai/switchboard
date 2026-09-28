"""Plain-text rendering of outbound messages (spec §8).

Everything is sent as plain text (no ``parse_mode``), so user-supplied text
can never be interpreted as Telegram HTML/Markdown.
"""

from __future__ import annotations

# Telegram's limit is 4096 UTF-16 code units; keep a margin.
MAX_TEXT_UTF16 = 4000
_ELLIPSIS = "…"


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units (how Telegram counts)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def truncate_utf16(text: str, limit: int = MAX_TEXT_UTF16) -> str:
    """Truncate to at most ``limit`` UTF-16 units, appending an ellipsis if cut.

    Cuts happen on code point boundaries, so surrogate pairs are never split.
    """
    if utf16_len(text) <= limit:
        return text
    budget = limit - utf16_len(_ELLIPSIS)
    used = 0
    end = 0
    for index, ch in enumerate(text):
        width = 2 if ord(ch) > 0xFFFF else 1
        if used + width > budget:
            break
        used += width
        end = index + 1
    return text[:end] + _ELLIPSIS


def format_event(severity: str | None, source: str, title: str | None, body: str | None) -> str:
    """``[SEVERITY] SOURCE — TITLE`` + newline + body; title part omitted if absent."""
    header = f"[{(severity or 'info').upper()}] {source}"
    if title:
        header += f" — {title}"
    return truncate_utf16(f"{header}\n{body or ''}".rstrip("\n"))


def format_outbound(row: dict | object) -> str:
    """Render a ``messages`` row: events get the header, replies are sent as-is."""
    get = row.__getitem__  # works for sqlite3.Row and dict
    if get("kind") == "reply":
        return truncate_utf16(get("body") or "")
    return format_event(get("severity"), get("source"), get("title"), get("body"))
