"""§10 check 2: same Idempotency-Key → same id, no new row; changed content → 409."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app.api.messages import IdempotencyConflict, enqueue_event
from app.api.models import MessageIn
from app.clock import FakeClock
from app.db.connection import Database
from tests.conftest import TOKEN_B, auth, rows, valid_event


def test_repeat_returns_original_id(api: TestClient, db: Database) -> None:
    headers = {**auth(), "Idempotency-Key": "job-482-done"}
    first = api.post("/api/v1/messages", json=valid_event(), headers=headers)
    second = api.post("/api/v1/messages", json=valid_event(), headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert len(rows(db)) == 1


def test_changed_content_same_key_conflicts(api: TestClient, db: Database) -> None:
    headers = {**auth(), "Idempotency-Key": "k1"}
    assert api.post("/api/v1/messages", json=valid_event(), headers=headers).status_code == 202
    response = api.post("/api/v1/messages", json=valid_event(message="different"),
                        headers=headers)
    assert response.status_code == 409
    assert len(rows(db)) == 1


def test_keys_are_scoped_per_source(api: TestClient, db: Database) -> None:
    a = api.post("/api/v1/messages", json=valid_event(),
                 headers={**auth(), "Idempotency-Key": "shared"})
    b = api.post("/api/v1/messages", json=valid_event(),
                 headers={**auth(TOKEN_B), "Idempotency-Key": "shared"})
    assert a.json()["id"] != b.json()["id"]
    assert {r["source"] for r in rows(db)} == {"research", "backup"}


def test_replay_reports_current_status(api: TestClient, db: Database) -> None:
    headers = {**auth(), "Idempotency-Key": "k2"}
    message_id = api.post("/api/v1/messages", json=valid_event(), headers=headers).json()["id"]
    with db.transaction() as conn:
        conn.execute("UPDATE messages SET status = 'sent' WHERE id = ?", (message_id,))
    replay = api.post("/api/v1/messages", json=valid_event(), headers=headers)
    assert replay.json() == {"id": message_id, "status": "sent"}


def test_concurrent_duplicates_create_one_row(db: Database, clock: FakeClock) -> None:
    event = MessageIn.model_validate(valid_event())
    results: list[str] = []
    errors: list[Exception] = []

    def submit() -> None:
        try:
            results.append(enqueue_event(db, clock, "research", event, "race").message_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(set(results)) == 1
    assert len(rows(db)) == 1


def test_conflict_detected_on_direct_call(db: Database, clock: FakeClock) -> None:
    enqueue_event(db, clock, "research", MessageIn.model_validate(valid_event()), "k")
    try:
        enqueue_event(db, clock, "research",
                      MessageIn.model_validate(valid_event(severity="error")), "k")
    except IdempotencyConflict:
        pass
    else:
        raise AssertionError("expected IdempotencyConflict")
