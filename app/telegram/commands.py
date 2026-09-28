"""Command parsing and reply texts (spec §7).

Supported, exactly: ``/help``, ``/status``, ``/test status``. A ``@botname``
suffix is accepted when it names this bot. Any other ``/command`` (including
known commands with unexpected arguments) is "unknown" and answered with help.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.services.test_service import ServiceStatus

CommandName = Literal["help", "status", "test_status", "unknown", "other_bot", "not_command"]

# Commands that need an outbound service call (handled with persisted state).
SERVICE_COMMANDS = frozenset({"status", "test_status"})

_COMMAND_TOKEN_RE = re.compile(r"/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?")

HELP_TEXT = (
    "Switchboard commands:\n"
    "/help – list commands\n"
    "/status – Switchboard uptime, queue and test service reachability\n"
    "/test status – check the test service"
)


@dataclass(frozen=True)
class ParsedCommand:
    name: CommandName
    # Canonical form stored in the audit row, e.g. "/test status". Never raw text.
    normalized: str | None


def parse_command(text: str | None, bot_username: str | None) -> ParsedCommand:
    if not text or not text.lstrip().startswith("/"):
        return ParsedCommand("not_command", None)

    tokens = text.split()
    match = _COMMAND_TOKEN_RE.fullmatch(tokens[0])
    if match is None:
        return ParsedCommand("unknown", "(unknown)")
    command, suffix = match.group(1), match.group(2)
    if suffix is not None and (bot_username is None or suffix.lower() != bot_username.lower()):
        return ParsedCommand("other_bot", None)

    args = tokens[1:]
    if command == "help" and not args:
        return ParsedCommand("help", "/help")
    if command == "status" and not args:
        return ParsedCommand("status", "/status")
    if command == "test" and args == ["status"]:
        return ParsedCommand("test_status", "/test status")
    return ParsedCommand("unknown", "(unknown)")


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    return f"{hours}h {minutes:02d}m {secs:02d}s"


def help_reply() -> str:
    return HELP_TEXT


def unknown_reply() -> str:
    return "Unknown command.\n\n" + HELP_TEXT


def status_reply(uptime_s: float, queued: int, dead: int, service: ServiceStatus) -> str:
    return (
        "Switchboard status\n"
        f"Uptime: {format_duration(uptime_s)}\n"
        f"Queued: {queued}\n"
        f"Dead letters: {dead}\n"
        f"Test service: {'reachable' if service.reachable else 'not reachable'}"
        f" ({service.summary})"
    )


def test_status_reply(service: ServiceStatus) -> str:
    return f"Test service: {service.summary}"


def interrupted_reply() -> str:
    return "The previous request was interrupted before it finished. Please retry."


RATE_LIMITED_REPLY = "Too many commands. Please wait a minute."
