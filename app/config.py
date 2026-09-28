"""Settings loading and validation (spec §4).

Secrets come from environment variables or from files named by a matching
``*_FILE`` variable (for Docker/systemd secrets). Validation collects every
problem and raises one :class:`ConfigError` naming the offending settings —
never their values, so secrets cannot leak into startup output.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PREFIX = "SWITCHBOARD_"

DEFAULT_DATABASE_PATH = "/volumes/data/switchboard.sqlite3"
DEFAULT_LOG_DIR = "/volumes/logs"
DEFAULT_TELEGRAM_API_BASE = "https://api.telegram.org"

_SOURCE_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_MIN_TOKEN_LENGTH = 32


class ConfigError(Exception):
    """Raised when required settings are missing or malformed."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True)
class Settings:
    """Validated, immutable runtime configuration."""

    database_path: Path
    bot_token: str = field(repr=False)
    telegram_user_id: int
    telegram_chat_id: int
    # source name -> bearer token
    producer_keys: dict[str, str] = field(repr=False)
    test_service_url: str | None = None
    test_service_token: str | None = field(default=None, repr=False)
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    log_dir: Path | None = None
    poll_timeout: int = 30
    telegram_api_base: str = DEFAULT_TELEGRAM_API_BASE

    def secrets(self) -> list[str]:
        """All secret strings, used by the log redaction filter."""
        values = [self.bot_token, *self.producer_keys.values()]
        if self.test_service_token:
            values.append(self.test_service_token)
        return [v for v in values if v]

    def telegram_api_is_https(self) -> bool:
        return urlsplit(self.telegram_api_base).scheme == "https"

    def bind_is_loopback(self) -> bool:
        if self.bind_host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.bind_host).is_loopback
        except ValueError:
            return False


def _read_secret(env: Mapping[str, str], name: str, problems: list[str]) -> str | None:
    """Read ``NAME`` or the file referenced by ``NAME_FILE`` (file wins)."""
    file_var = f"{name}_FILE"
    file_path = env.get(file_var)
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip() or None
        except OSError:
            problems.append(f"{file_var}: cannot read file")
            return None
    value = env.get(name)
    return value.strip() if value and value.strip() else None


def _parse_int(env: Mapping[str, str], name: str, problems: list[str], *, required: bool,
               default: int | None = None) -> int | None:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        if required:
            problems.append(f"{name}: required")
        return default
    try:
        return int(raw.strip())
    except ValueError:
        problems.append(f"{name}: must be an integer")
        return default


def _parse_producer_keys(env: Mapping[str, str], problems: list[str]) -> dict[str, str]:
    """Load the source -> token map from JSON (file or inline variable)."""
    name = f"{PREFIX}PRODUCER_KEYS"
    raw = _read_secret(env, name, problems)
    if raw is None:
        problems.append(f"{name} (or {name}_FILE): required")
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        problems.append(f"{name}: must be a JSON object of source name to token")
        return {}
    if not isinstance(data, dict) or not data:
        problems.append(f"{name}: must be a non-empty JSON object")
        return {}

    keys: dict[str, str] = {}
    for source, token in data.items():
        if not isinstance(source, str) or not _SOURCE_NAME_RE.fullmatch(source):
            problems.append(f"{name}: invalid source name (must match [a-z][a-z0-9_-]{{0,63}})")
            continue
        if source == "switchboard":
            # Reserved: Switchboard's own replies use this source name.
            problems.append(f"{name}: source name 'switchboard' is reserved")
            continue
        if not isinstance(token, str) or len(token) < _MIN_TOKEN_LENGTH:
            problems.append(
                f"{name}: token for source '{source}' must be at least "
                f"{_MIN_TOKEN_LENGTH} characters"
            )
            continue
        if not token.isascii() or not token.isprintable() or " " in token:
            problems.append(f"{name}: token for source '{source}' has invalid characters")
            continue
        keys[source] = token
    if len(set(keys.values())) != len(keys):
        problems.append(f"{name}: every source must have a distinct token")
    return keys


def _parse_service_url(env: Mapping[str, str], problems: list[str]) -> str | None:
    name = f"{PREFIX}TEST_SERVICE_URL"
    raw = env.get(name, "").strip()
    if not raw:
        return None
    return _parse_http_url(name, raw, problems)


def _parse_http_url(name: str, raw: str, problems: list[str]) -> str | None:
    """Validate a base URL that fixed routes get appended to (never the value in errors)."""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        problems.append(f"{name}: must be an http(s) URL with a host")
        return None
    try:
        parts.port  # noqa: B018 - raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        problems.append(f"{name}: invalid port")
        return None
    if parts.query or parts.fragment or parts.username or parts.password:
        problems.append(f"{name}: must not contain query, fragment or credentials")
        return None
    # Normalize: no trailing slash, so we can append fixed routes safely.
    return raw.rstrip("/")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from ``env`` (defaults to ``os.environ``)."""
    env = os.environ if env is None else env
    problems: list[str] = []

    bot_token = _read_secret(env, f"{PREFIX}BOT_TOKEN", problems)
    if bot_token is None:
        problems.append(f"{PREFIX}BOT_TOKEN (or {PREFIX}BOT_TOKEN_FILE): required")
    elif not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", bot_token):
        problems.append(f"{PREFIX}BOT_TOKEN: does not look like a Telegram bot token")

    user_id = _parse_int(env, f"{PREFIX}TELEGRAM_USER_ID", problems, required=True)
    chat_id = _parse_int(env, f"{PREFIX}TELEGRAM_CHAT_ID", problems, required=True)
    producer_keys = _parse_producer_keys(env, problems)
    test_service_url = _parse_service_url(env, problems)
    test_service_token = _read_secret(env, f"{PREFIX}TEST_SERVICE_TOKEN", problems)

    bind_host = env.get(f"{PREFIX}BIND_HOST", "").strip() or "127.0.0.1"
    bind_port = _parse_int(env, f"{PREFIX}BIND_PORT", problems, required=False, default=8080)
    if bind_port is not None and not 1 <= bind_port <= 65535:
        problems.append(f"{PREFIX}BIND_PORT: must be between 1 and 65535")

    poll_timeout = _parse_int(env, f"{PREFIX}POLL_TIMEOUT", problems, required=False, default=30)
    if poll_timeout is not None and not 1 <= poll_timeout <= 50:
        problems.append(f"{PREFIX}POLL_TIMEOUT: must be between 1 and 50 seconds")

    database_path = Path(env.get(f"{PREFIX}DATABASE_PATH", "").strip() or DEFAULT_DATABASE_PATH)
    # An explicitly empty SWITCHBOARD_LOG_DIR disables file logging (stdout only).
    log_dir_raw = env.get(f"{PREFIX}LOG_DIR", DEFAULT_LOG_DIR).strip()
    log_dir = Path(log_dir_raw) if log_dir_raw else None

    api_base = _parse_http_url(
        f"{PREFIX}TELEGRAM_API_BASE",
        env.get(f"{PREFIX}TELEGRAM_API_BASE", "").strip() or DEFAULT_TELEGRAM_API_BASE,
        problems,
    )

    if problems:
        raise ConfigError(problems)

    assert bot_token is not None and user_id is not None and chat_id is not None
    assert api_base is not None
    return Settings(
        database_path=database_path,
        bot_token=bot_token,
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        producer_keys=producer_keys,
        test_service_url=test_service_url,
        test_service_token=test_service_token,
        bind_host=bind_host,
        bind_port=bind_port or 8080,
        log_dir=log_dir,
        poll_timeout=poll_timeout or 30,
        telegram_api_base=api_base,
    )
