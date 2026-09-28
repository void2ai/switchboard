"""§10 check 3: the worker delivers to the authorized chat; DB records sent + message id."""

from __future__ import annotations

import json

import respx

from app.api.messages import enqueue_event
from app.api.models import MessageIn
from app.clock import FakeClock
from app.db.connection import Database
from app.delivery.worker import DeliveryWorker
from tests.conftest import OWNER_ID, TG_BOT, rows, sent_ok, valid_event


async def test_worker_sends_and_records(db: Database, clock: FakeClock, worker: DeliveryWorker,
                                        mock_http: respx.MockRouter) -> None:
    route = mock_http.post(f"{TG_BOT}/sendMessage").mock(return_value=sent_ok(42))
    outcome = enqueue_event(db, clock, "research", MessageIn.model_validate(valid_event()), None)

    assert await worker.process_one() is True
    assert await worker.process_one() is False  # nothing else due

    payload = json.loads(route.calls.last.request.content)
    assert payload["chat_id"] == OWNER_ID
    assert payload["text"] == "[INFO] research — Research complete\nFound 6 useful papers."
    assert "parse_mode" not in payload  # plain text only

    (row,) = rows(db)
    assert row["id"] == outcome.message_id
    assert row["status"] == "sent"
    assert row["telegram_message_id"] == 42
    assert row["telegram_chat_id"] == OWNER_ID
    assert row["sent_at"] is not None and row["attempts"] == 1
    assert row["lease_until"] is None


async def test_markup_is_not_interpreted(db: Database, clock: FakeClock, worker: DeliveryWorker,
                                         mock_http: respx.MockRouter) -> None:
    route = mock_http.post(f"{TG_BOT}/sendMessage").mock(return_value=sent_ok())
    enqueue_event(db, clock, "research",
                  MessageIn.model_validate(valid_event(message="<b>*bold*</b> [x](y)")), None)
    await worker.process_one()
    payload = json.loads(route.calls.last.request.content)
    assert payload["text"].endswith("<b>*bold*</b> [x](y)")
    assert "parse_mode" not in payload


async def test_oldest_message_is_sent_first(db: Database, clock: FakeClock,
                                            worker: DeliveryWorker,
                                            mock_http: respx.MockRouter) -> None:
    route = mock_http.post(f"{TG_BOT}/sendMessage").mock(return_value=sent_ok())
    for n in range(3):
        enqueue_event(db, clock, "research",
                      MessageIn.model_validate(valid_event(message=f"msg {n}")), None)
        clock.advance(1)
    while await worker.process_one():
        pass
    texts = [json.loads(c.request.content)["text"].rsplit("\n", 1)[1] for c in route.calls]
    assert texts == ["msg 0", "msg 1", "msg 2"]
