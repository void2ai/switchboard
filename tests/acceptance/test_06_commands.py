"""§10 check 6: /status reports state, /test status reaches the test service, and a
timed-out or misbehaving service yields a short, safe error reply."""

from __future__ import annotations

import httpx
import respx

from app.api.messages import enqueue_event
from app.api.models import MessageIn
from app.clock import FakeClock
from app.db.connection import Database
from app.telegram.poller import TelegramPoller
from tests.conftest import SERVICE_URL, make_update, rows, valid_event


def _reply(db: Database) -> dict:
    (reply,) = rows(db, "direction = 'outbound' AND kind = 'reply'")
    return reply


def _inbound(db: Database) -> dict:
    (inbound,) = rows(db, "direction = 'inbound'")
    return inbound


async def test_status_reports_switchboard_state(poller: TelegramPoller, db: Database,
                                                clock: FakeClock,
                                                mock_http: respx.MockRouter) -> None:
    mock_http.get(f"{SERVICE_URL}/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "name": "test-service"}))
    for _ in range(2):
        enqueue_event(db, clock, "research", MessageIn.model_validate(valid_event()), None)
    clock.advance(3723)

    await poller.process_update(make_update(1, "/status"))
    body = _reply(db)["body"]
    assert "Uptime: 1h 02m 03s" in body
    assert "Queued: 2" in body  # the reply itself was not queued yet when counted
    assert "Dead letters: 0" in body
    assert "Test service: reachable (ok (test-service))" in body
    assert _inbound(db)["status"] == "handled"


async def test_test_status_success(poller: TelegramPoller, db: Database,
                                   mock_http: respx.MockRouter) -> None:
    route = mock_http.get(f"{SERVICE_URL}/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "name": "test-service"}))
    await poller.process_update(make_update(1, "/test status"))
    assert route.call_count == 1
    assert _reply(db)["body"] == "Test service: ok (test-service)"
    inbound = _inbound(db)
    assert inbound["status"] == "handled" and inbound["body"] == "/test status"


async def test_test_status_timeout(poller: TelegramPoller, db: Database,
                                   mock_http: respx.MockRouter) -> None:
    mock_http.get(f"{SERVICE_URL}/health").mock(side_effect=httpx.ReadTimeout("slow"))
    await poller.process_update(make_update(1, "/test status"))
    assert _reply(db)["body"] == "Test service: timed out"
    inbound = _inbound(db)
    assert inbound["status"] == "failed" and inbound["last_error_code"] == "service_timeout"


async def test_test_status_unreachable(poller: TelegramPoller, db: Database,
                                       mock_http: respx.MockRouter) -> None:
    mock_http.get(f"{SERVICE_URL}/health").mock(
        side_effect=httpx.ConnectError(f"cannot connect to {SERVICE_URL}"))
    await poller.process_update(make_update(1, "/test status"))
    body = _reply(db)["body"]
    assert body == "Test service: unreachable"
    assert "testsvc" not in body


async def test_raw_service_body_is_never_forwarded(poller: TelegramPoller, db: Database,
                                                   mock_http: respx.MockRouter) -> None:
    mock_http.get(f"{SERVICE_URL}/health").mock(return_value=httpx.Response(
        200, text="Traceback (most recent call last): secret=hunter2"))
    await poller.process_update(make_update(1, "/test status"))
    body = _reply(db)["body"]
    assert body == "Test service: invalid response"
    assert "hunter2" not in body and "Traceback" not in body


async def test_oversized_service_body(poller: TelegramPoller, db: Database,
                                      mock_http: respx.MockRouter) -> None:
    mock_http.get(f"{SERVICE_URL}/health").mock(
        return_value=httpx.Response(200, content=b"{" + b" " * 10_000 + b"}"))
    await poller.process_update(make_update(1, "/test status"))
    assert _reply(db)["body"] == "Test service: response too large"


async def test_redirect_is_not_followed(poller: TelegramPoller, db: Database,
                                        mock_http: respx.MockRouter) -> None:
    mock_http.get(f"{SERVICE_URL}/health").mock(
        return_value=httpx.Response(302, headers={"Location": "http://evil.test/"}))
    evil = mock_http.get("http://evil.test/")
    await poller.process_update(make_update(1, "/test status"))
    assert evil.call_count == 0
    assert _reply(db)["body"] == "Test service: HTTP 302"


async def test_unexpected_arguments_get_help(poller: TelegramPoller, db: Database,
                                             mock_http: respx.MockRouter) -> None:
    route = mock_http.get(f"{SERVICE_URL}/health")
    await poller.process_update(make_update(1, "/test status now"))
    await poller.process_update(make_update(2, "/test http://evil.test/"))
    assert route.call_count == 0
    replies = rows(db, "direction = 'outbound'")
    assert all(r["body"].startswith("Unknown command.") for r in replies)
