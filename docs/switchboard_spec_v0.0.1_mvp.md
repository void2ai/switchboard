# Switchboard — MVP specification v0.0.1

Status: verified MVP

Date: 2026-09-28

Supersedes: v0.0.0; folds accepted decisions D2 and D9 into the specification

## 1. Purpose and scope

Switchboard is a small daemon connecting software services to a human through Telegram. Services submit generic notifications to a local HTTP API. Switchboard persists, formats, and delivers them. One allowlisted Telegram user can issue a small set of commands that Switchboard handles or forwards to one registered test service.

The MVP proves both directions with a single instance, single Telegram bot, single destination, single authorized user, and one deliberately simple test service. Telegram is an adapter; producers never need bot credentials or Telegram chat IDs.

### MVP outcomes

1. An authenticated service can submit an event; the server commits it to SQLite before acknowledging it.
2. A worker delivers queued events to Telegram and retries transient failures.
3. The authorized user can send `/help`, `/status`, and `/test status` in a private chat.
4. `/test status` calls a registered test service and returns its response.
5. Inbound commands, outbound attempts, and outcomes are recorded without secrets.
6. A health endpoint reports process and database health.

### Deferred

Web dashboard, other transports, multiple users or destinations, arbitrary service registration, AI interpretation, scheduler, remote administration, destructive commands, command confirmations, distributed workers, and service discovery. No Redis, RabbitMQ, or Kafka.

## 2. Technology and deployment

- Python, FastAPI, SQLite in WAL mode, parameterized raw SQL, one background delivery worker, and the official Telegram Bot API over HTTPS.
- All application SQL lives in `app/db/queries.py`; DDL lives in `app/db/schema.sql`.
- Single process/instance owning the SQLite queue and Telegram polling loop. Start background tasks with application lifespan; stop them cleanly on shutdown. Do not run multiple Uvicorn workers or replicas in the MVP.
- Use Telegram `getUpdates` long polling for inbound messages. Delete any previously configured webhook during setup, since polling and webhooks cannot be active together. Persist update offsets before accepting later updates; see §7 for processing details.
- HTTP listener binds to loopback by default. If a container network or LAN requires another bind address, require a trusted network boundary and service authentication. Never expose the API unauthenticated to the public internet.
- Persistent paths: `/volumes/data/switchboard.sqlite3`, `/volumes/config/`, `/volumes/logs/`; paths are configurable. Database backup must use SQLite's backup API or equivalent WAL-aware procedure.

## 3. Boundaries and message flow

Outbound: service → authenticated `POST /api/v1/messages` → SQLite transaction → queue worker → Telegram `sendMessage` → status update.

Inbound: Telegram `getUpdates` → numeric user/chat allowlist → command parser → local handler or configured test service → queued Telegram reply.

Events report something that happened. Commands request an action or information. Service code does not import Switchboard internals; Switchboard has no application business logic.

## 4. Configuration

Read secrets from environment or protected secret files; non-secret routing may be in a local config file. Fail startup with a clear error if required values are absent or malformed.

| Setting | Purpose |
| --- | --- |
| `SWITCHBOARD_DATABASE_PATH` | SQLite path |
| `SWITCHBOARD_BOT_TOKEN` | Telegram bot token |
| `SWITCHBOARD_TELEGRAM_USER_ID` | Authorized numeric sender ID |
| `SWITCHBOARD_TELEGRAM_CHAT_ID` | Authorized private chat ID and outbound destination |
| `SWITCHBOARD_PRODUCER_KEYS` | Map of source names to distinct bearer tokens, loaded securely |
| `SWITCHBOARD_TEST_SERVICE_URL` | Fixed base URL of test service |
| `SWITCHBOARD_TEST_SERVICE_TOKEN` | Credential for test service, if enabled |
| `SWITCHBOARD_BIND_HOST`, `SWITCHBOARD_BIND_PORT` | Listener, default loopback |

Do not log tokens, Authorization headers, raw incoming request bodies, or sensitive event payloads. Rotate credentials by replacing configuration and restarting in MVP.

## 5. Public HTTP contract

### `POST /api/v1/messages`

Request: `Authorization: Bearer <producer token>`, `Content-Type: application/json`, optional `Idempotency-Key` (1–128 printable ASCII characters). The token maps to exactly one configured source. Do not accept a caller-supplied source as authority.

```json
{
  "destination": "owner",
  "kind": "event",
  "topic": "research.completed",
  "severity": "info",
  "title": "Research complete",
  "message": "Found 6 useful papers.",
  "correlation_id": "job-482"
}
```

Required: `destination`, `kind`, `topic`, `severity`, `message`. `destination` must be `owner`; `kind` must be `event`. `topic` matches `^[a-z][a-z0-9_.-]{0,79}$`; `severity` is `info`, `warning`, or `error`; `title` is optional (max 120 characters); `message` is plain text, 1–3000 characters; `correlation_id` is optional (max 128 characters). Reject extra fields and oversized HTTP bodies (16 KiB). Never interpolate user text into Telegram HTML or Markdown; send plain text.

Success after database commit: `202 Accepted`:

```json
{"id":"<uuid>","status":"pending"}
```

If `Idempotency-Key` repeats for the same authenticated source, return the original ID/status and create no new delivery; if the same key has different validated content, return `409 Conflict`. Retain keys as long as message records remain. Missing or invalid token returns `401`; invalid payload `422`; unavailable database `503`. An accepted request means durable enqueue, not Telegram delivery.

### `GET /health`

Return `200 {"status":"ok"}` only when the process is running and SQLite can execute a read/write readiness probe. Otherwise `503`. Do not require the producer token for a loopback health probe; expose no configuration or message data. Telegram availability belongs in logs, not this response.

The readiness probe updates and commits the singleton `health_probe` row. It
therefore proves that the database is writable, not merely readable or
lockable.

## 6. Data model

Initialize SQLite with WAL, busy timeout, foreign keys, and migrations recorded by a schema version. All timestamps are UTC ISO 8601. Use transactions for state transitions.

`messages`: `id` UUID primary key; `direction` (`inbound`/`outbound`); `source`; `destination`; `kind`; `topic`; `severity`; `title`; `body`; `correlation_id`; `status` (`pending`, `sending`, `sent`, `retry`, `dead`, `received`, `processing`, `handled`, `failed`, `ignored`); `attempts` integer; `next_attempt_at`; `lease_until`; `telegram_chat_id`; `telegram_message_id`; `created_at`; `sent_at`; `last_error_code`. Store only fields needed to route, retry, and audit. Inbound rows must not contain arbitrary chat transcripts. A service-backed inbound command moves `received` → `processing` → `handled` or `failed`; owner text that is not a command uses `ignored`.

`idempotency_keys`: `(source, key)` unique; `request_hash`; `message_id` foreign key.

`telegram_state`: singleton row with `next_update_id`. Advance this in the same transaction that records an authorized command or a safely ignored update and queues any reply. Replayed updates must not execute a command twice.

`health_probe`: singleton row with `id=1` and `checked_at`. `/health` updates
this row in a transaction and commits before returning 200.

Indexes: delivery by `(status, next_attempt_at)` and recent messages by `created_at`; source/key unique index.

## 7. Telegram inbound commands

- Poll only private chats. Compare numeric `from.id` and `chat.id` with configured IDs before recording or replying. Ignore all other users/chats silently; do not use usernames for authorization.
- Support exact `/help`, `/status`, `/test status`; allow the bot name suffix on commands addressed to this bot. Unknown commands from the owner receive a concise help response. Reject unexpected arguments for MVP.
- `/help` lists supported commands. `/status` returns Switchboard uptime, queued count, and test service reachability. `/test status` makes `GET <configured test service URL>/health` with a short timeout and returns a bounded, sanitized status summary; it has no side effects.
- For service calls, use a fixed configured base URL, fixed route, explicit timeout, and optional bearer credential. Never build URLs from Telegram text. Do not follow redirects to arbitrary hosts.
- A command handler must commit its inbound record, offset, and outbound reply/job intent before processing the next update. For a service call, Tx A records the inbound command as `processing` and advances the offset; the network call then runs without a database transaction; Tx B queues the reply and marks the inbound row `handled`, or `failed` for a safe error reply. On restart, resume an unfinished read-only call or mark it failed and queue a safe error reply. Never duplicate side-effecting commands in MVP.
- Limit response text to Telegram's supported message size with room for formatting; truncate safely. Do not include raw service responses, secrets, or stack traces.

## 8. Outbound delivery and failure behavior

- Worker claims due `pending`/`retry` rows in a transaction with a lease. Only one worker exists in MVP. On restart, expired `sending` leases become eligible again.
- Format plain text as `[SEVERITY] SOURCE — TITLE` followed by body; omit absent title. Bound total length to Telegram's limit.
- On success, record `sent`, Telegram message ID, and `sent_at`. On network error, timeout, HTTP 5xx, or Telegram 429, increment attempts and schedule exponential backoff with jitter; honor Telegram `retry_after` where supplied. Default delay 5 seconds, doubling to a maximum of 15 minutes; stop after 10 attempts and mark `dead` with a safe error code.
- Permanent Telegram errors (invalid chat, blocked bot, malformed request) mark `dead` promptly and surface a clear log entry. Avoid retry storms.
- Delivery is **at least once**, not exactly once: a crash after Telegram accepts a message but before the DB commits `sent` can produce a duplicate. Document this tradeoff in user-facing deployment notes.
- SQLite commits and network calls are never held in the same transaction. Queue survives Telegram outage and process restart. If SQLite is unavailable, reject new messages instead of claiming success.

## 9. Security and observability

- Restrict service tokens to configured source identity; compare tokens safely; use TLS across untrusted networks. Never accept unauthenticated producers.
- Telegram numeric allowlist is the only inbound user authorization in MVP. No restart, deploy, delete, shell execution, arbitrary URL fetch, or natural-language command routing.
- Use size limits, request timeouts, and modest rate limiting for HTTP ingestion and Telegram commands. Do not render untrusted content as HTML/Markdown.
- Structured log lines include timestamp, request ID or message ID, source, event/command name, transition, elapsed time, and safe error code. Omit bot tokens and message bodies by default.
- Surface queue depth and dead-letter count via logs; the health endpoint stays minimal.

## 10. Test service and verification

Ship a tiny standalone test service with `GET /health` returning bounded JSON such as `{"status":"ok","name":"test-service"}`. It has no Telegram dependency and no mutating command. Configure Switchboard to call only this service in the MVP.

The MVP is done when these checks pass:

1. Submit a valid authenticated message: HTTP 202 only after persistence; exactly one queue row exists. Wrong token, bad payload, and wrong destination are rejected.
2. Repeat with the same idempotency key: same ID, no extra row; changed content with same key yields 409.
3. Worker sends an event to the authorized private Telegram chat; database records `sent` and message ID.
4. Simulate Telegram outage: accepted message remains queued, attempts and delay increase, and delivery resumes after recovery or process restart. Demonstrate dead-letter behavior after retry exhaustion.
5. Unknown Telegram user/chat gets no reply and cannot invoke the test service. Authorized `/help` works.
6. Authorized `/status` reports Switchboard state. Authorized `/test status` reaches the test service and replies. A timed-out test service yields a short safe error reply.
7. Replay the same Telegram update: no duplicate service call or reply intent. Health reports failure when the database is unusable.

## 11. Suggested build sequence

1. Skeleton, configuration validation, schema/migration, and database query functions.
2. Producer authentication, HTTP enqueue contract, and idempotency.
3. Delivery worker, Telegram outbound, lease/retry handling.
4. Telegram polling, allowlist, command state, `/help`, `/status`.
5. Test service, `/test status`, logs, and end-to-end verification.

Future adapters may implement the same destination/event contract without changing producers. Add real services one at a time after the MVP checks pass.
