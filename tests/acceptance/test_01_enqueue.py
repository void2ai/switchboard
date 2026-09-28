"""§10 check 1: authenticated submit → 202 only after persistence; bad requests rejected."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.connection import Database
from tests.conftest import TOKEN_A, auth, rows, valid_event


def test_valid_message_is_persisted_before_202(api: TestClient, db: Database) -> None:
    response = api.post("/api/v1/messages", json=valid_event(), headers=auth())
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending" and len(body["id"]) == 36

    stored = rows(db)  # fresh connection: proves the row is committed
    assert len(stored) == 1
    row = stored[0]
    assert row["id"] == body["id"]
    assert row["source"] == "research"  # from the token, not the payload
    assert row["direction"] == "outbound" and row["status"] == "pending"
    assert row["body"] == "Found 6 useful papers." and row["attempts"] == 0


def test_title_and_correlation_are_optional(api: TestClient) -> None:
    event = valid_event()
    del event["title"], event["correlation_id"]
    assert api.post("/api/v1/messages", json=event, headers=auth()).status_code == 202


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-token-" + "x" * 30},
     {"Authorization": TOKEN_A}, {"Authorization": f"Basic {TOKEN_A}"}],
)
def test_bad_tokens_are_rejected(api: TestClient, db: Database, headers: dict) -> None:
    response = api.post("/api/v1/messages", json=valid_event(), headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert rows(db) == []


@pytest.mark.parametrize(
    "payload",
    [
        valid_event(destination="all"),
        valid_event(kind="command"),
        valid_event(source="someone-else"),  # caller-supplied source is an extra field
        valid_event(extra="x"),
        valid_event(topic="Research"),
        valid_event(topic="research.completed\n"),
        valid_event(topic="x" * 81),
        valid_event(severity="critical"),
        valid_event(severity=1),
        valid_event(title="t" * 121),
        valid_event(message=""),
        valid_event(message="   "),
        valid_event(message="m" * 3001),
        valid_event(message="bell\x07"),
        valid_event(title="evil‮override"),
        valid_event(correlation_id="c" * 129),
        {k: v for k, v in valid_event().items() if k != "message"},
    ],
)
def test_invalid_payloads_are_rejected(api: TestClient, db: Database, payload: dict) -> None:
    response = api.post("/api/v1/messages", json=payload, headers=auth())
    assert response.status_code == 422
    # Error details name fields/types only; no input values echoed back.
    assert "someone-else" not in response.text and "mmmm" not in response.text
    assert rows(db) == []


def test_boundary_values_are_accepted(api: TestClient) -> None:
    event = valid_event(topic="a" + "b" * 79, title="t" * 120, message="m" * 3000,
                        correlation_id="c" * 128)
    assert api.post("/api/v1/messages", json=event, headers=auth()).status_code == 202


def test_malformed_json_and_non_object(api: TestClient) -> None:
    headers = {**auth(), "Content-Type": "application/json"}
    assert api.post("/api/v1/messages", content=b"{not json", headers=headers).status_code == 422
    assert api.post("/api/v1/messages", content=b"[1,2]", headers=headers).status_code == 422
    assert api.post("/api/v1/messages", content=b"\xff\xfe", headers=headers).status_code == 422


def test_wrong_content_type(api: TestClient) -> None:
    response = api.post("/api/v1/messages", content=json.dumps(valid_event()),
                        headers={**auth(), "Content-Type": "text/plain"})
    assert response.status_code == 415


def test_oversized_body_is_rejected(api: TestClient, db: Database) -> None:
    big = valid_event(message="m" * 3000)
    big["padding"] = "p" * (17 * 1024)
    response = api.post("/api/v1/messages", json=big, headers=auth())
    assert response.status_code == 413
    assert rows(db) == []


def test_oversized_chunked_body_is_rejected(api: TestClient) -> None:
    def chunks():
        for _ in range(20):
            yield b"x" * 1024

    response = api.post("/api/v1/messages", content=chunks(),
                        headers={**auth(), "Content-Type": "application/json"})
    assert response.status_code == 413


def test_invalid_idempotency_key(api: TestClient) -> None:
    for key in ["", "k" * 129, "tab\tkey"]:
        response = api.post("/api/v1/messages", json=valid_event(),
                            headers={**auth(), "Idempotency-Key": key})
        assert response.status_code == 422, key


def test_database_unavailable_returns_503(api: TestClient, settings: Settings) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = settings.database_path.with_name(settings.database_path.name + suffix)
        path.unlink(missing_ok=True)
    response = api.post("/api/v1/messages", json=valid_event(), headers=auth())
    assert response.status_code == 503
    assert response.json() == {"error": "database_unavailable"}
    # And no empty database was silently created in its place.
    assert not settings.database_path.exists()


def test_rate_limit_per_source(api: TestClient) -> None:
    codes = [
        api.post("/api/v1/messages", json=valid_event(), headers=auth()).status_code
        for _ in range(25)
    ]
    assert codes[:20] == [202] * 20
    assert 429 in codes[20:]
