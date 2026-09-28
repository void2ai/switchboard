"""Full application run: lifespan starts the worker and poller; Telegram and the test
service are mocked at the HTTP layer. Also verifies that no secret or message body
reaches the log file, and that a second instance cannot start."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.logs import configure_logging
from app.main import InstanceLockError, create_app
from tests.conftest import (
    BOT_TOKEN,
    SERVICE_URL,
    TG_BOT,
    TOKEN_A,
    auth,
    env_for,
    make_update,
    sent_ok,
    valid_event,
)

SECRET_BODY = "Found 6 useful papers about zebra finches."


@pytest.fixture
def e2e_settings(tmp_path: Path) -> Settings:
    return load_settings(env_for(tmp_path, SWITCHBOARD_LOG_DIR=str(tmp_path / "logs")))


@pytest.fixture
def captured_logs(e2e_settings: Settings) -> Iterator[Path]:
    root = logging.getLogger()
    before = list(root.handlers)
    configure_logging(e2e_settings.secrets(), e2e_settings.log_dir)
    yield e2e_settings.log_dir / "switchboard.log"  # type: ignore[operator]
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_full_round_trip(e2e_settings: Settings, captured_logs: Path) -> None:
    sent_texts: list[str] = []
    polls = {"n": 0}

    def send_message(request: httpx.Request) -> httpx.Response:
        sent_texts.append(json.loads(request.content)["text"])
        return sent_ok(len(sent_texts))

    async def get_updates(request: httpx.Request) -> httpx.Response:
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(200, json={"ok": True, "result": [
                make_update(1, "/test status"),
                make_update(2, "/help", user_id=999, chat_id=999),  # stranger
            ]})
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"ok": True, "result": []})

    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{TG_BOT}/getMe").mock(return_value=httpx.Response(
            200, json={"ok": True, "result": {"username": "SwitchboardBot"}}))
        mock.post(f"{TG_BOT}/deleteWebhook").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True}))
        mock.post(f"{TG_BOT}/getUpdates").mock(side_effect=get_updates)
        mock.post(f"{TG_BOT}/sendMessage").mock(side_effect=send_message)
        service = mock.get(f"{SERVICE_URL}/health").mock(return_value=httpx.Response(
            200, json={"status": "ok", "name": "test-service"}))

        app = create_app(e2e_settings)
        with TestClient(app) as client:
            response = client.post("/api/v1/messages", json=valid_event(message=SECRET_BODY),
                                   headers=auth())
            assert response.status_code == 202
            _wait_for(lambda: len(sent_texts) >= 2)
            assert client.get("/health").status_code == 200

    assert service.call_count == 1
    assert "[INFO] research — Research complete\n" + SECRET_BODY in sent_texts
    assert "Test service: ok (test-service)" in sent_texts
    assert len(sent_texts) == 2  # the stranger got nothing

    log_text = captured_logs.read_text()
    assert "delivery.sent" in log_text and "telegram.command" in log_text
    assert BOT_TOKEN not in log_text
    assert TOKEN_A not in log_text
    assert SECRET_BODY not in log_text
    for line in log_text.splitlines():
        json.loads(line)  # every line is valid JSON


def test_second_instance_is_refused(e2e_settings: Settings) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{TG_BOT}/getMe").mock(return_value=httpx.Response(503))
        mock.post(f"{TG_BOT}/getUpdates").mock(return_value=httpx.Response(503))
        with TestClient(create_app(e2e_settings)):
            with pytest.raises(InstanceLockError):
                with TestClient(create_app(e2e_settings)):
                    pass


def test_api_accepts_events_while_telegram_is_down(e2e_settings: Settings) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__startswith=TG_BOT).mock(side_effect=httpx.ConnectError("down"))
        with TestClient(create_app(e2e_settings)) as client:
            response = client.post("/api/v1/messages", json=valid_event(), headers=auth())
            assert response.status_code == 202
            assert client.get("/health").status_code == 200
