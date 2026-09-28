"""Unit tests for pure helpers: config, backoff, formatting, parsing, sanitizing, logging."""

from __future__ import annotations

import io
import json
import logging
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import ProducerAuth
from app.clock import FakeClock
from app.config import ConfigError, load_settings
from app.db.connection import MigrationError, validate_migration_names
from app.delivery.backoff import MAX_DELAY_S, base_delay, retry_delay
from app.delivery.formatting import format_event, format_outbound, truncate_utf16, utf16_len
from app.logs import JsonFormatter, RedactionFilter, get_logger, log_event
from app.main import create_app
from app.ratelimit import TokenBucket
from app.services.test_service import summarize_health
from app.telegram.client import classify_response
from app.telegram.commands import format_duration, parse_command
from tests.conftest import BOT_TOKEN, TOKEN_A, TOKEN_B, env_for

# --- config ---------------------------------------------------------------


def test_config_loads_valid_env(tmp_path: Path) -> None:
    settings = load_settings(env_for(tmp_path))
    assert settings.producer_keys == {"research": TOKEN_A, "backup": TOKEN_B}
    assert settings.bind_host == "127.0.0.1" and settings.bind_is_loopback()
    assert BOT_TOKEN not in repr(settings) and TOKEN_A not in repr(settings)


def test_config_reports_all_problems_without_values(tmp_path: Path) -> None:
    env = env_for(tmp_path, SWITCHBOARD_TELEGRAM_USER_ID="abc",
                  SWITCHBOARD_PRODUCER_KEYS=json.dumps({"x": "short"}))
    del env["SWITCHBOARD_BOT_TOKEN"]
    with pytest.raises(ConfigError) as err:
        load_settings(env)
    text = str(err.value)
    assert "SWITCHBOARD_BOT_TOKEN" in text
    assert "SWITCHBOARD_TELEGRAM_USER_ID" in text
    assert "at least 32" in text
    assert "short" not in text  # never echo secret values


def test_config_rejects_duplicate_tokens_and_reserved_source(tmp_path: Path) -> None:
    env = env_for(tmp_path, SWITCHBOARD_PRODUCER_KEYS=json.dumps(
        {"a": TOKEN_A, "b": TOKEN_A, "switchboard": TOKEN_B}))
    with pytest.raises(ConfigError) as err:
        load_settings(env)
    assert "distinct" in str(err.value) and "reserved" in str(err.value)


def test_config_reads_secret_files(tmp_path: Path) -> None:
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({"svc": TOKEN_A}))
    token_file = tmp_path / "bot"
    token_file.write_text(BOT_TOKEN + "\n")
    env = env_for(tmp_path, SWITCHBOARD_PRODUCER_KEYS_FILE=str(keys_file),
                  SWITCHBOARD_BOT_TOKEN_FILE=str(token_file), SWITCHBOARD_BOT_TOKEN="")
    settings = load_settings(env)
    assert settings.producer_keys == {"svc": TOKEN_A}
    assert settings.bot_token == BOT_TOKEN


def test_config_rejects_service_url_with_query(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(env_for(tmp_path, SWITCHBOARD_TEST_SERVICE_URL="http://h/x?y=1"))


@pytest.mark.parametrize("value", [
    "https://", "ftp://x", "api.telegram.org", "https://u:p@host", "https://host/?a=1",
    "https://host/#f", "https://host:99999", "https://host:abc",
])
def test_config_rejects_bad_telegram_api_base(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError) as err:
        load_settings(env_for(tmp_path, SWITCHBOARD_TELEGRAM_API_BASE=value))
    assert "SWITCHBOARD_TELEGRAM_API_BASE" in str(err.value)
    assert value not in str(err.value)


@pytest.mark.parametrize(("value", "expected", "https"), [
    ("", "https://api.telegram.org", True),
    ("https://api.telegram.org/", "https://api.telegram.org", True),
    ("http://127.0.0.1:8081/prefix/", "http://127.0.0.1:8081/prefix", False),
])
def test_config_accepts_telegram_api_base(
    tmp_path: Path, value: str, expected: str, https: bool
) -> None:
    settings = load_settings(env_for(tmp_path, SWITCHBOARD_TELEGRAM_API_BASE=value))
    assert settings.telegram_api_base == expected
    assert settings.telegram_api_is_https() is https


@pytest.mark.parametrize(("base", "warned"), [
    ("http://127.0.0.1:8081", True), ("https://telegram.test", False),
])
def test_startup_warns_on_plain_http_telegram_api_base(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, base: str, warned: bool
) -> None:
    settings = load_settings(env_for(tmp_path, SWITCHBOARD_TELEGRAM_API_BASE=base))
    with caplog.at_level(logging.WARNING), \
            TestClient(create_app(settings, start_background=False)):
        pass
    events = [getattr(r, "event", r.getMessage()) for r in caplog.records]
    assert any("insecure_telegram_api_base" in str(e) for e in events) is warned
    assert base not in caplog.text


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_env_example_is_sourceable() -> None:
    # The README Quick start sources the example with the shell, which strips
    # unquoted double quotes; the JSON values must survive that.
    example = Path(__file__).parents[2] / "volumes" / "config" / ".env.example"
    bash = shutil.which("bash")
    assert bash is not None
    out = subprocess.run(  # noqa: S603 - fixed argv, repo file only
        [bash, "-c", 'set -a; . "$1"; set +a; env -0', "_", str(example)],
        env={}, capture_output=True, check=True,
    ).stdout.decode()
    env = dict(item.split("=", 1) for item in out.split("\0") if "=" in item)
    settings = load_settings(env)
    assert set(settings.producer_keys) == {"research"}


# --- auth -----------------------------------------------------------------


def test_auth_maps_token_to_source() -> None:
    auth = ProducerAuth({"research": TOKEN_A, "backup": TOKEN_B})
    assert auth.authenticate(f"Bearer {TOKEN_A}") == "research"
    assert auth.authenticate(f"bearer {TOKEN_B}") == "backup"
    assert auth.authenticate(None) is None
    assert auth.authenticate("Bearer ") is None
    assert auth.authenticate(f"Basic {TOKEN_A}") is None
    assert auth.authenticate(f"Bearer {TOKEN_A}x") is None
    assert auth.authenticate("Bearer tökén") is None


# --- backoff --------------------------------------------------------------


def test_backoff_doubles_and_caps() -> None:
    assert [base_delay(n) for n in range(1, 5)] == [5, 10, 20, 40]
    assert base_delay(9) == 900 and base_delay(10) == MAX_DELAY_S
    assert base_delay(10_000) == MAX_DELAY_S


def test_backoff_jitter_range_and_retry_after() -> None:
    rng = random.Random(1)
    for attempt in range(1, 12):
        d = retry_delay(attempt, None, rng)
        assert base_delay(attempt) / 2 <= d <= base_delay(attempt)
    assert retry_delay(1, 120, rng) == 120


# --- formatting -----------------------------------------------------------


def test_format_event_header() -> None:
    assert format_event("warning", "research", "Done", "body") == "[WARNING] research — Done\nbody"
    assert format_event("info", "research", None, "body") == "[INFO] research\nbody"


def test_format_reply_is_plain_body() -> None:
    assert format_outbound({"kind": "reply", "body": "hello"}) == "hello"


def test_truncate_counts_utf16_and_never_splits_pairs() -> None:
    emoji = "\U0001F600"  # 2 UTF-16 units
    text = emoji * 3000
    out = truncate_utf16(text, 4000)
    assert utf16_len(out) <= 4000
    assert out.endswith("…")
    assert all(ch == emoji for ch in out[:-1])
    assert truncate_utf16("short") == "short"


# --- command parsing ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/help", "help"),
        ("/help@SwitchboardBot", "help"),
        ("/help@switchboardbot", "help"),
        ("/status", "status"),
        ("/test status", "test_status"),
        ("  /test   status  ", "test_status"),
        ("/test@SwitchboardBot status", "test_status"),
        ("/help me", "unknown"),
        ("/status now", "unknown"),
        ("/test", "unknown"),
        ("/test foo", "unknown"),
        ("/test status now", "unknown"),
        ("/HELP", "unknown"),
        ("/", "unknown"),
        ("/rm -rf", "unknown"),
        ("/help@OtherBot", "other_bot"),
        ("hello", "not_command"),
        ("", "not_command"),
        (None, "not_command"),
    ],
)
def test_parse_command(text: str | None, expected: str) -> None:
    assert parse_command(text, "SwitchboardBot").name == expected


def test_parse_command_normalized_never_contains_raw_text() -> None:
    parsed = parse_command("/secret-stuff with args", "Bot")
    assert parsed.normalized == "(unknown)"


def test_format_duration() -> None:
    assert format_duration(3723) == "1h 02m 03s"
    assert format_duration(90061) == "1d 01h 01m"


# --- service response sanitizing -----------------------------------------


def test_summarize_health() -> None:
    assert summarize_health(b'{"status":"ok","name":"test-service"}').summary == \
        "ok (test-service)"
    assert summarize_health(b'{"status":"starting"}').state == "degraded"
    assert summarize_health(b"not json").state == "invalid_response"
    assert summarize_health(b"[]").state == "invalid_response"
    # Unsafe values are never echoed.
    bad = summarize_health(b'{"status":"<b>x</b>","name":"n"}')
    assert bad.state == "invalid_response" and "<b>" not in bad.summary
    odd_name = summarize_health(b'{"status":"ok","name":"Traceback (most recent call)\\n"}')
    assert odd_name.summary == "ok"


# --- Telegram classification ---------------------------------------------


def test_classify_response() -> None:
    assert classify_response(200, {"ok": True, "result": {"message_id": 1}}).outcome == "ok"
    assert classify_response(200, None).outcome == "transient"
    r = classify_response(429, {"ok": False, "parameters": {"retry_after": 17}})
    assert r.outcome == "rate_limited" and r.retry_after == 17
    assert classify_response(502, None).outcome == "transient"
    assert classify_response(401, {"ok": False}).outcome == "auth_failed"
    assert classify_response(404, {"ok": False}).outcome == "auth_failed"
    assert classify_response(409, {"ok": False}).error_code == "telegram_conflict"
    r = classify_response(400, {"ok": False, "description": "Bad Request: chat not found"})
    assert r.outcome == "permanent" and r.error_code == "chat_not_found"
    blocked = {"ok": False, "description": "Forbidden: bot was blocked by the user"}
    r = classify_response(403, blocked)
    assert r.error_code == "bot_blocked"


# --- rate limiting --------------------------------------------------------


def test_token_bucket() -> None:
    clock = FakeClock()
    bucket = TokenBucket(60, 2, clock)
    assert bucket.try_acquire()[0] and bucket.try_acquire()[0]
    allowed, wait = bucket.try_acquire()
    assert not allowed and wait == 1
    clock.advance(1.0)
    assert bucket.try_acquire()[0]


# --- logging --------------------------------------------------------------


def test_redaction_drops_fields_and_scrubs_secrets() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactionFilter([BOT_TOKEN, TOKEN_A]))
    handler.setFormatter(JsonFormatter())
    logger = get_logger("test.redaction")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        log_event(logger, logging.INFO, "x.event", body="secret body", message="m",
                  authorization=f"Bearer {TOKEN_A}", url=f"https://t/bot{BOT_TOKEN}/send",
                  source="research")
        logger.info("plain %s", BOT_TOKEN)
        try:
            raise RuntimeError(f"failed calling https://t/bot{BOT_TOKEN}/getUpdates")
        except RuntimeError:
            logger.exception("boom")
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert BOT_TOKEN not in output and TOKEN_A not in output
    assert "secret body" not in output
    first = json.loads(output.splitlines()[0])
    assert first["event"] == "x.event" and first["source"] == "research"
    assert "body" not in first and "authorization" not in first
    assert "[REDACTED]" in first["url"]


# --- migration file names -------------------------------------------------


def test_migrations_none_needed_at_version_1() -> None:
    assert validate_migration_names([], 1) == {}
    assert validate_migration_names(["__init__.py", "README.md"], 1) == {}


def test_migrations_valid_sequence() -> None:
    names = ["003_add_index.sql", "002_retention.sql", "__init__.py"]
    assert validate_migration_names(names, 3) == {2: "002_retention.sql", 3: "003_add_index.sql"}


@pytest.mark.parametrize(("names", "version", "expected"), [
    (["2_short.sql"], 2, "NNN_description"),
    (["002-dash.sql"], 2, "NNN_description"),
    (["002_Upper.sql"], 2, "NNN_description"),
    (["002_a.sql", "002_b.sql"], 2, "duplicate number 2"),
    (["001_again.sql"], 1, "start at 2"),
    (["002_a.sql"], 1, "above SCHEMA_VERSION"),  # added a migration, forgot to bump
    ([], 2, "missing migration(s) for version(s) [2]"),  # bumped, forgot the file
    (["002_a.sql", "004_c.sql"], 4, "version(s) [3]"),  # gap
])
def test_migrations_invalid(names: list[str], version: int, expected: str) -> None:
    with pytest.raises(MigrationError) as err:
        validate_migration_names(names, version)
    assert expected in str(err.value)


def test_migrations_reports_all_problems() -> None:
    with pytest.raises(MigrationError) as err:
        validate_migration_names(["bad.sql", "002_a.sql", "002_b.sql"], 3)
    assert len(err.value.problems) == 3  # bad name, duplicate, missing 3
