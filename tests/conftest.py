"""Shared fixtures: temp database, settings, fake clock, mocked Telegram and test service."""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.clock import FakeClock
from app.config import Settings, load_settings
from app.db.connection import Database
from app.delivery.worker import DeliveryWorker
from app.main import create_app
from app.services.test_service import TestServiceClient
from app.telegram.client import TelegramClient
from app.telegram.poller import TelegramPoller

BOT_TOKEN = "123456:TEST-bot_token_value"
TG_BASE = "https://telegram.test"
TG_BOT = f"{TG_BASE}/bot{BOT_TOKEN}"
SERVICE_URL = "http://testsvc.test"
OWNER_ID = 1111
OTHER_ID = 2222
TOKEN_A = "a" * 20 + "-producer-token-A-xyz"
TOKEN_B = "b" * 20 + "-producer-token-B-xyz"


def env_for(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "SWITCHBOARD_DATABASE_PATH": str(tmp_path / "data" / "switchboard.sqlite3"),
        "SWITCHBOARD_LOG_DIR": "",
        "SWITCHBOARD_BOT_TOKEN": BOT_TOKEN,
        "SWITCHBOARD_TELEGRAM_USER_ID": str(OWNER_ID),
        "SWITCHBOARD_TELEGRAM_CHAT_ID": str(OWNER_ID),
        "SWITCHBOARD_PRODUCER_KEYS": json.dumps({"research": TOKEN_A, "backup": TOKEN_B}),
        "SWITCHBOARD_TEST_SERVICE_URL": SERVICE_URL,
        "SWITCHBOARD_TELEGRAM_API_BASE": TG_BASE,
    }
    env.update(overrides)
    return env


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env_for(tmp_path))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


@pytest.fixture
def api(settings: Settings, clock: FakeClock) -> Iterator[TestClient]:
    """HTTP API only (no worker/poller), with the lifespan run."""
    app = create_app(settings, clock=clock, start_background=False)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def mock_http() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def http_client() -> Any:
    async with httpx.AsyncClient(follow_redirects=False) as client:
        yield client


@pytest.fixture
def telegram(http_client: httpx.AsyncClient) -> TelegramClient:
    return TelegramClient(BOT_TOKEN, http_client, TG_BASE)


@pytest.fixture
def worker(db: Database, telegram: TelegramClient, clock: FakeClock) -> DeliveryWorker:
    return DeliveryWorker(db, telegram, OWNER_ID, clock, rng=random.Random(42))


@pytest.fixture
def poller(db: Database, telegram: TelegramClient, http_client: httpx.AsyncClient,
           settings: Settings, clock: FakeClock) -> TelegramPoller:
    service = TestServiceClient(settings.test_service_url, settings.test_service_token,
                                http_client)
    p = TelegramPoller(db, telegram, service, settings, clock, lambda: None,
                       clock.monotonic())
    p.bot_username = "SwitchboardBot"
    return p


def valid_event(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "destination": "owner",
        "kind": "event",
        "topic": "research.completed",
        "severity": "info",
        "title": "Research complete",
        "message": "Found 6 useful papers.",
        "correlation_id": "job-482",
    }
    body.update(overrides)
    return body


def auth(token: str = TOKEN_A) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_update(update_id: int, text: str | None = "/help", *, user_id: int = OWNER_ID,
                chat_id: int = OWNER_ID, chat_type: str = "private") -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id * 10,
        "from": {"id": user_id, "is_bot": False, "first_name": "X"},
        "chat": {"id": chat_id, "type": chat_type},
        "date": 1_700_000_000,
    }
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def rows(db: Database, where: str = "1=1", params: tuple = ()) -> list[dict[str, Any]]:
    """Test helper: read messages rows as dicts."""
    with db.reader() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY created_at, rowid",  # noqa: S608
            params,
        ).fetchall()]


def sent_ok(message_id: int = 42) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})
