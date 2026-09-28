"""Telegram long polling and command handling (spec §7).

Processing guarantees:

* Updates are handled strictly one at a time, in order.
* Every update ends in a transaction that advances ``telegram_state.next_update_id``
  together with whatever it recorded (inbound row, queued reply). A replayed
  update (same ``update_id``) is therefore skipped, never executed twice.
* Unauthorized updates are not recorded and never answered; only the offset moves.
* Commands that call a service use two transactions around the call:

  - Tx A: inbound row ``processing`` + offset advance (commit)
  - HTTP call (no transaction open)
  - Tx B: inbound ``handled``/``failed`` + queued reply (commit)

  If the process dies between A and B, :meth:`TelegramPoller.recover_unfinished`
  finishes the command on the next start. The call is read-only, so re-running
  it is safe, and Telegram will not redeliver the update because the offset
  was already committed in Tx A.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import uuid
from collections.abc import Callable
from typing import Any

from app.clock import Clock
from app.config import Settings
from app.db import queries
from app.db.connection import Database
from app.delivery.backoff import MAX_DELAY_S
from app.logs import get_logger, log_event
from app.ratelimit import TokenBucket
from app.services.test_service import TestServiceClient
from app.telegram import commands
from app.telegram.client import TelegramClient, TelegramResult

log = get_logger(__name__)

POLL_ERROR_MAX_DELAY_S = 60.0
COMMAND_RATE_PER_MINUTE = 20
COMMAND_BURST = 10
RATE_NOTICE_INTERVAL_S = 60.0


class TelegramPoller:
    def __init__(
        self,
        db: Database,
        telegram: TelegramClient,
        test_service: TestServiceClient,
        settings: Settings,
        clock: Clock,
        wake_worker: Callable[[], None],
        started_monotonic: float,
    ) -> None:
        self._db = db
        self._telegram = telegram
        self._test_service = test_service
        self._settings = settings
        self._clock = clock
        self._wake_worker = wake_worker
        self._started = started_monotonic
        self._limiter = TokenBucket(COMMAND_RATE_PER_MINUTE, COMMAND_BURST, clock)
        self._rate_notice_after = 0.0
        self.bot_username: str | None = None

    # --- Main loop --------------------------------------------------------

    async def run(self) -> None:
        await self._setup()

        delay = 1.0
        # Startup recovery runs inside the guarded loop, so a database error
        # there is retried instead of killing the poller task.
        needs_recovery = True
        while True:
            try:
                if needs_recovery:
                    # An earlier error may have left a command 'processing'
                    # (Tx A committed, Tx B not). Finish it before moving on.
                    await self.recover_unfinished()
                    needs_recovery = False
                offset = await asyncio.to_thread(self._read_offset)
                result = await self._telegram.get_updates(offset, self._settings.poll_timeout)
                if not result.ok:
                    await self._poll_failed(result, delay)
                    delay = min(delay * 2, POLL_ERROR_MAX_DELAY_S)
                    continue
                delay = 1.0
                updates = result.data if isinstance(result.data, list) else []
                for update in updates:
                    await self.process_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the poller must never die
                # The offset was not advanced for the failing update, so it is
                # fetched again on the next iteration.
                log_event(log, logging.ERROR, "telegram.poll_error",
                          error_code=type(exc).__name__)
                needs_recovery = True
                await asyncio.sleep(_jitter(delay))
                delay = min(delay * 2, POLL_ERROR_MAX_DELAY_S)

    async def _poll_failed(self, result: TelegramResult, delay: float) -> None:
        if result.outcome == "auth_failed":
            log_event(log, logging.ERROR, "telegram.auth_failed", error_code=result.error_code)
            await asyncio.sleep(MAX_DELAY_S)
            return
        level = logging.ERROR if result.error_code == "telegram_conflict" else logging.WARNING
        log_event(log, level, "telegram.poll_failed", error_code=result.error_code)
        wait = result.retry_after if result.retry_after else _jitter(delay)
        await asyncio.sleep(wait)

    async def _setup(self) -> None:
        """Learn the bot username (for ``/cmd@bot``) and delete any webhook.

        Retries forever: a Telegram outage at startup must not stop the HTTP
        API from accepting and queueing events.
        """
        delay = 1.0
        while True:
            result = await self._telegram.get_me()
            if result.ok and isinstance(result.data, dict):
                username = result.data.get("username")
                webhook = await self._telegram.delete_webhook()
                if webhook.ok:
                    self.bot_username = username if isinstance(username, str) else None
                    log_event(log, logging.INFO, "telegram.ready")
                    return
                result = webhook
            log_event(log, logging.ERROR if result.outcome == "auth_failed" else logging.WARNING,
                      "telegram.setup_failed", error_code=result.error_code)
            await asyncio.sleep(MAX_DELAY_S if result.outcome == "auth_failed" else _jitter(delay))
            delay = min(delay * 2, POLL_ERROR_MAX_DELAY_S)

    def _read_offset(self) -> int:
        with self._db.reader() as conn:
            return queries.get_next_update_id(conn)

    # --- Per update -------------------------------------------------------

    async def process_update(self, update: Any) -> None:
        """Handle one update from ``getUpdates``. Safe to call again with the same update."""
        if not isinstance(update, dict) or not _is_int(update.get("update_id")):
            log_event(log, logging.WARNING, "telegram.update_ignored", reason="malformed")
            return
        update_id: int = update["update_id"]

        if update_id < await asyncio.to_thread(self._read_offset):
            log_event(log, logging.INFO, "telegram.update_ignored", update_id=update_id,
                      reason="already_processed")
            return

        message = update.get("message")
        reason = self._authorization_failure(message)
        if reason is not None:
            await asyncio.to_thread(self._advance_only, update_id)
            log_event(log, logging.WARNING, "telegram.update_ignored", update_id=update_id,
                      reason=reason)
            return

        text = message.get("text") if isinstance(message.get("text"), str) else None
        parsed = commands.parse_command(text, self.bot_username)

        if parsed.name in ("not_command", "other_bot"):
            topic = "message.text" if text is not None else "message.other"
            await asyncio.to_thread(self._record_local, update_id, "update", topic, None,
                                    "ignored", None)
            log_event(log, logging.INFO, "telegram.update_ignored", update_id=update_id,
                      reason=parsed.name)
            return

        allowed, _ = self._limiter.try_acquire()
        if not allowed:
            await self._handle_rate_limited(update_id)
            return

        log_event(log, logging.INFO, "telegram.command", update_id=update_id,
                  command=parsed.normalized)

        if parsed.name in commands.SERVICE_COMMANDS:
            inbound_id = await asyncio.to_thread(self._begin_service_command, update_id, parsed)
            if inbound_id is not None:
                await self._complete_service_command(inbound_id, parsed.name)
            return

        reply = commands.help_reply() if parsed.name == "help" else commands.unknown_reply()
        await asyncio.to_thread(self._record_local, update_id, "command",
                                f"command.{parsed.name}", parsed.normalized, "handled", reply)
        self._wake_worker()

    def _authorization_failure(self, message: Any) -> str | None:
        """Return a reason code if the update must be ignored, else None.

        Authorization uses the numeric sender and chat IDs only — never usernames.
        """
        if not isinstance(message, dict):
            return "no_message"
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return "not_private"
        if not isinstance(sender, dict) or not _is_int(sender.get("id")) \
                or sender["id"] != self._settings.telegram_user_id:
            return "unauthorized_user"
        if not _is_int(chat.get("id")) or chat["id"] != self._settings.telegram_chat_id:
            return "unauthorized_chat"
        return None

    async def _handle_rate_limited(self, update_id: int) -> None:
        now = self._clock.monotonic()
        reply = None
        if now >= self._rate_notice_after:
            # One notice per window, so rate limiting cannot itself flood the chat.
            self._rate_notice_after = now + RATE_NOTICE_INTERVAL_S
            reply = commands.RATE_LIMITED_REPLY
        await asyncio.to_thread(self._record_local, update_id, "command", "command.rate_limited",
                                None, "ignored", reply)
        log_event(log, logging.WARNING, "telegram.update_ignored", update_id=update_id,
                  reason="rate_limited")
        if reply:
            self._wake_worker()

    # --- Transactions -----------------------------------------------------

    def _advance_only(self, update_id: int) -> None:
        with self._db.transaction() as conn:
            queries.advance_next_update_id(conn, update_id + 1)

    def _record_local(self, update_id: int, kind: str, topic: str, body: str | None,
                      status: str, reply: str | None) -> None:
        """One transaction: inbound row + offset advance + optional queued reply."""
        with self._db.transaction() as conn:
            if queries.get_next_update_id(conn) > update_id:
                return  # processed concurrently/earlier; do nothing twice
            inbound_id = self._insert_inbound(conn, update_id, kind, topic, body, status)
            queries.advance_next_update_id(conn, update_id + 1)
            if reply is not None:
                self._insert_reply(conn, inbound_id, topic, reply)

    def _begin_service_command(self, update_id: int,
                               parsed: commands.ParsedCommand) -> str | None:
        """Tx A: persist the command as ``processing`` and advance the offset."""
        with self._db.transaction() as conn:
            if queries.get_next_update_id(conn) > update_id:
                return None
            inbound_id = self._insert_inbound(conn, update_id, "command",
                                              f"command.{parsed.name}", parsed.normalized,
                                              "processing")
            queries.advance_next_update_id(conn, update_id + 1)
            return inbound_id

    async def _complete_service_command(self, inbound_id: str, name: str) -> None:
        """Run the read-only service call, then Tx B: finish row + queue reply."""
        service = await self._test_service.check_health()
        final_status, error_code = "handled", None
        if name == "status":
            counts = await asyncio.to_thread(self._counts)
            reply = commands.status_reply(
                self._clock.monotonic() - self._started,
                counts["pending"] + counts["retry"] + counts["sending"],
                counts["dead"],
                service,
            )
        else:
            reply = commands.test_status_reply(service)
            if not service.reachable:
                final_status, error_code = "failed", f"service_{service.state}"
        await asyncio.to_thread(self._finish_command, inbound_id, f"command.{name}",
                                final_status, error_code, reply)
        self._wake_worker()

    def _finish_command(self, inbound_id: str, topic: str, status: str,
                        error_code: str | None, reply: str) -> None:
        with self._db.transaction() as conn:
            # Compare-and-set: only the first completion queues a reply.
            if queries.transition_inbound(conn, inbound_id, "processing", status, error_code):
                self._insert_reply(conn, inbound_id, topic, reply)

    def _counts(self) -> dict[str, int]:
        with self._db.reader() as conn:
            return queries.outbound_status_counts(conn)

    def _insert_inbound(self, conn: sqlite3.Connection, update_id: int, kind: str, topic: str,
                        body: str | None, status: str) -> str:
        inbound_id = str(uuid.uuid4())
        queries.insert_message(
            conn,
            id=inbound_id,
            direction="inbound",
            source="telegram",
            destination="switchboard",
            kind=kind,
            topic=topic,
            body=body,  # normalized command only; never the raw chat text
            status=status,
            telegram_chat_id=self._settings.telegram_chat_id,
            telegram_update_id=update_id,
            created_at=self._clock.now_ts(),
        )
        return inbound_id

    def _insert_reply(self, conn: sqlite3.Connection, inbound_id: str, topic: str,
                      text: str) -> None:
        now = self._clock.now_ts()
        queries.insert_message(
            conn,
            id=str(uuid.uuid4()),
            direction="outbound",
            source="switchboard",
            destination="owner",
            kind="reply",
            topic=topic,
            severity="info",
            body=text,
            correlation_id=inbound_id,
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )

    # --- Startup recovery -------------------------------------------------

    async def recover_unfinished(self) -> None:
        """Finish service commands interrupted between Tx A and Tx B."""
        rows = await asyncio.to_thread(self._list_processing)
        for row in rows:
            name = row["topic"].removeprefix("command.")
            log_event(log, logging.WARNING, "telegram.command_recovered", message_id=row["id"],
                      command=row["body"])
            if name in commands.SERVICE_COMMANDS:
                await self._complete_service_command(row["id"], name)
            else:
                await asyncio.to_thread(self._finish_command, row["id"], row["topic"], "failed",
                                        "interrupted", commands.interrupted_reply())
                self._wake_worker()

    def _list_processing(self) -> list[sqlite3.Row]:
        with self._db.reader() as conn:
            return queries.list_processing_inbound(conn)


def _is_int(value: Any) -> bool:
    # bool is a subclass of int; a JSON true must not pass as an ID.
    return isinstance(value, int) and not isinstance(value, bool)


def _jitter(delay: float) -> float:
    return random.uniform(delay / 2, delay)
