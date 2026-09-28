"""§10 check 5: unknown users/chats get no reply and cannot reach the test service;
authorized /help works."""

from __future__ import annotations

import pytest
import respx

from app.db import queries
from app.db.connection import Database
from app.telegram.poller import TelegramPoller
from tests.conftest import OTHER_ID, OWNER_ID, SERVICE_URL, make_update, rows


def _offset(db: Database) -> int:
    with db.reader() as conn:
        return queries.get_next_update_id(conn)


@pytest.mark.parametrize(
    "update",
    [
        make_update(1, "/test status", user_id=OTHER_ID, chat_id=OTHER_ID),
        make_update(1, "/test status", user_id=OWNER_ID, chat_id=OTHER_ID),
        make_update(1, "/test status", user_id=OTHER_ID, chat_id=OWNER_ID),
        make_update(1, "/test status", chat_type="group"),
        make_update(1, "/test status", chat_type="supergroup"),
        {"update_id": 1, "edited_message": make_update(1)["message"]},
        {"update_id": 1, "message": {"text": "/help", "chat": {"id": OWNER_ID}}},  # no from
        {"update_id": 1, "message": {"text": "/help", "from": {"id": True},
                                     "chat": {"id": OWNER_ID, "type": "private"}}},
    ],
)
async def test_unauthorized_updates_are_ignored(poller: TelegramPoller, db: Database,
                                                mock_http: respx.MockRouter,
                                                update: dict) -> None:
    service = mock_http.get(f"{SERVICE_URL}/health")
    await poller.process_update(update)
    assert rows(db) == []  # nothing recorded, nothing queued
    assert service.call_count == 0
    assert _offset(db) == 2  # but the update is consumed


async def test_owner_help_is_answered(poller: TelegramPoller, db: Database) -> None:
    await poller.process_update(make_update(5, "/help"))
    inbound, reply = rows(db)
    assert inbound["direction"] == "inbound" and inbound["status"] == "handled"
    assert inbound["body"] == "/help" and inbound["telegram_update_id"] == 5
    assert reply["direction"] == "outbound" and reply["kind"] == "reply"
    assert reply["status"] == "pending" and reply["correlation_id"] == inbound["id"]
    assert "/test status" in reply["body"]
    assert _offset(db) == 6


async def test_help_with_bot_suffix(poller: TelegramPoller, db: Database) -> None:
    await poller.process_update(make_update(1, "/help@SwitchboardBot"))
    assert [r["status"] for r in rows(db)] == ["handled", "pending"]


async def test_command_for_other_bot_is_ignored(poller: TelegramPoller, db: Database) -> None:
    await poller.process_update(make_update(1, "/help@OtherBot"))
    (row,) = rows(db)
    assert row["status"] == "ignored" and row["body"] is None


async def test_plain_text_is_ignored_and_not_stored(poller: TelegramPoller,
                                                    db: Database) -> None:
    await poller.process_update(make_update(1, "my private thoughts"))
    await poller.process_update(make_update(2, None))  # e.g. a photo
    rows_ = rows(db)
    assert [r["status"] for r in rows_] == ["ignored", "ignored"]
    assert all(r["body"] is None for r in rows_)
    assert [r["topic"] for r in rows_] == ["message.text", "message.other"]
    assert _offset(db) == 3


async def test_unknown_command_gets_help(poller: TelegramPoller, db: Database) -> None:
    await poller.process_update(make_update(1, "/deploy prod --force"))
    inbound, reply = rows(db)
    assert inbound["body"] == "(unknown)"  # raw text is never stored
    assert reply["body"].startswith("Unknown command.")


async def test_command_rate_limit(poller: TelegramPoller, db: Database) -> None:
    for n in range(1, 14):
        await poller.process_update(make_update(n, "/help"))
    inbound = rows(db, "direction = 'inbound'")
    limited = [r for r in inbound if r["topic"] == "command.rate_limited"]
    assert len(limited) == 3  # burst of 10 allowed
    replies = rows(db, "direction = 'outbound'")
    # 10 help replies + exactly one "slow down" notice.
    assert len(replies) == 11
    assert sum("Too many commands" in r["body"] for r in replies) == 1
