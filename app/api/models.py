"""Request validation for ``POST /api/v1/messages`` (spec §5)."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TOPIC_RE = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
# Idempotency-Key: 1-128 printable ASCII characters (space through tilde).
IDEMPOTENCY_KEY_RE = re.compile(r"[\x20-\x7e]{1,128}")

_MESSAGE_ALLOWED_CONTROLS = frozenset("\n\t\r")


# Bidirectional embedding/override/isolate controls ("Trojan Source" chars).
# Other format chars (e.g. the zero-width joiner used in emoji) stay allowed.
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩")


def _has_control_chars(value: str, allowed: frozenset[str] = frozenset()) -> bool:
    """True if ``value`` contains control or bidi-override chars outside ``allowed``.

    Rejecting them keeps logs, audit rows and Telegram output free of invisible
    or terminal-manipulating characters.
    """
    return any(
        (unicodedata.category(ch) == "Cc" or ch in _BIDI_CONTROLS) and ch not in allowed
        for ch in value
    )


class MessageIn(BaseModel):
    """A producer event. Unknown fields are rejected (``extra="forbid"``)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    destination: Literal["owner"]
    kind: Literal["event"]
    topic: str
    severity: Literal["info", "warning", "error"]
    title: str | None = Field(default=None, max_length=120)
    message: str = Field(min_length=1, max_length=3000)
    correlation_id: str | None = Field(default=None, max_length=128)

    @field_validator("topic")
    @classmethod
    def _topic_format(cls, value: str) -> str:
        # fullmatch (not a pattern with $) so a trailing newline cannot slip through.
        if not TOPIC_RE.fullmatch(value):
            raise ValueError("topic must match ^[a-z][a-z0-9_.-]{0,79}$")
        return value

    @field_validator("title", "correlation_id")
    @classmethod
    def _no_controls(cls, value: str | None) -> str | None:
        if value is not None and _has_control_chars(value):
            raise ValueError("control characters are not allowed")
        return value

    @field_validator("message")
    @classmethod
    def _message_text(cls, value: str) -> str:
        if _has_control_chars(value, _MESSAGE_ALLOWED_CONTROLS):
            raise ValueError("control characters other than newline and tab are not allowed")
        if not value.strip():
            raise ValueError("message must not be blank")
        return value

    def request_hash(self) -> str:
        """SHA-256 over the canonical JSON of the *validated* payload.

        Used to detect an Idempotency-Key reused with different content.
        """
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def valid_idempotency_key(value: str) -> bool:
    return bool(IDEMPOTENCY_KEY_RE.fullmatch(value))
