# Switchboard — MVP implementation plan

Status: plan draft 1
Date: 2026-09-26
Implements: [switchboard_spec_v0.0.0_mvp.md](switchboard_spec_v0.0.0_mvp.md) (referred to below as "the spec", sections as §N)

This plan turns the spec into a build order, a file layout, concrete design decisions for points the spec leaves open, and a verification matrix that maps every §10 acceptance check to a test. No code exists yet; the repository currently holds only `docs/`.

---

## 0. Decisions to confirm before starting

The spec is implementation-ready for most of the surface, but these points are ambiguous or unspecified. Each has a recommended default that the rest of this plan assumes. Items marked **spec change** add something the spec doesn't list; confirm or reject those before milestone 1.

| # | Question | Recommended default |
| --- | --- | --- |
| D1 | Format of `SWITCHBOARD_PRODUCER_KEYS` | `SWITCHBOARD_PRODUCER_KEYS_FILE` points to a JSON file `{"source-name": "token", ...}` with mode `0600` in `/volumes/config/`. Also accept the inline env var `SWITCHBOARD_PRODUCER_KEYS` holding the same JSON, for dev. Startup fails if tokens are duplicated across sources, shorter than 32 chars, or source names don't match `^[a-z][a-z0-9_-]{0,63}$`. |
| D2 | How to track the state of an inbound command that calls a service (§7: "persist an inbound command state first") | **Spec change:** add inbound statuses `processing`, `handled`, `failed` to the `messages.status` enum. Inbound lifecycle: `received` → `processing` → `handled`/`failed`; `ignored` for owner updates that aren't commands. An alternative that avoids the change is inferring completion from a reply row whose `correlation_id` equals the inbound id. That works, but it's less explicit and harder to query. |
| D3 | Status code for an oversized body (§5 says "reject"; it doesn't name a code) | `413 Payload Too Large`. Malformed JSON and schema violations return `422`. |
| D4 | Response to an idempotent repeat | `202` with the original `id` and the message's **current** status (for example `sent`). No new row. |
| D5 | Telegram `401 Unauthorized` or `404` on the bot token (the whole bot is broken, not one message) | Treat as a fatal configuration error: don't dead-letter the queue. Pause the worker and poller, log `telegram.auth_failed` at ERROR, and keep retrying with the maximum backoff (15 min). Rows stay `pending`/`retry`. |
| D6 | How replies to commands are represented | Outbound `messages` rows with `source='switchboard'`, `kind='reply'`, `topic='command.<name>'`, `correlation_id=<inbound message id>`. Replies skip the `[SEVERITY] SOURCE — TITLE` header and are sent as the plain body. `kind='reply'` is internal only; the public API still accepts only `event`. |
| D7 | Owner sends plain text (not a command) | Record as inbound `ignored` (with no body stored), advance the offset, and send no reply. Unknown `/commands` get the help text, as the spec requires. |
| D8 | Unauthorized users or chats | Store no `messages` row. Advance `next_update_id` in its own transaction and log `telegram.update_ignored` with the reason `unauthorized` (no user text, no username). This satisfies §7: nothing is recorded or replied to before the allowlist check. |
| D9 | Health probe write | **Spec change:** add a singleton `health_probe(id=1, checked_at)` table. `/health` runs `UPDATE health_probe SET checked_at=?` in a transaction and commits. This proves that the database is writable, not only lockable. |
| D10 | Schema versioning | `PRAGMA user_version`. `schema.sql` is version 1. Later migrations go in `app/db/migrations/NNN_*.sql` and are applied in order inside a transaction at startup. |
| D11 | Python and tooling | Python 3.12, `uv` with `pyproject.toml`, dependencies `fastapi`, `uvicorn`, `httpx`, `pydantic` v2. Dev dependencies: `pytest`, `pytest-asyncio`, `respx`, `ruff`. No ORM. |
| D12 | Rate limits (§9: "modest") | Ingestion: an in-memory token bucket per source, 60 requests/min with a burst of 20, returning `429` with `Retry-After`. Commands: 20 commands/min per owner. Over-limit commands are recorded as `ignored`, with a single "slow down" reply per window. |

---

## 1. Target repository layout

```
switchboard/
├── pyproject.toml
├── README.md                      # quickstart + deployment notes (at-least-once, single instance, backup)
├── .env.example                   # every setting from spec §4, no real secrets
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app factory, lifespan (start/stop worker + poller)
│   ├── config.py                  # load and validate settings; fail fast with clear errors
│   ├── logging.py                 # structured JSON formatter + redaction filter
│   ├── ratelimit.py               # token bucket
│   ├── auth.py                    # bearer → source lookup, hmac.compare_digest
│   ├── api/
│   │   ├── models.py              # pydantic request/response models (extra="forbid")
│   │   ├── messages.py            # POST /api/v1/messages
│   │   ├── health.py              # GET /health
│   │   └── body_limit.py          # 16 KiB request-size middleware
│   ├── db/
│   │   ├── schema.sql             # all DDL (spec §2)
│   │   ├── queries.py             # ALL application SQL incl. PRAGMAs (spec §2)
│   │   ├── connection.py          # open connection, apply pragmas, run migrations (calls queries.py)
│   │   └── migrations/            # empty in MVP
│   ├── delivery/
│   │   ├── worker.py              # claim → send → record loop
│   │   ├── backoff.py             # pure delay computation
│   │   └── formatting.py          # event + reply text rendering, truncation
│   ├── telegram/
│   │   ├── client.py              # getMe, deleteWebhook, getUpdates, sendMessage; error classification
│   │   ├── poller.py              # long-poll loop, allowlist, offset persistence
│   │   └── commands.py            # parser + /help, /status, /test status handlers
│   └── services/
│       └── test_service.py        # fixed-URL GET /health client + response sanitizer
├── test_service/
│   └── main.py                    # standalone FastAPI app, GET /health only (spec §10)
├── scripts/
│   └── backup.py                  # WAL-aware backup using sqlite3.Connection.backup
├── deploy/
│   ├── Dockerfile
│   └── compose.example.yml        # switchboard + test_service, /volumes mounts, loopback/internal net
└── tests/
    ├── conftest.py                # temp DB, settings fixture, respx Telegram mock, fake clock
    ├── unit/                      # backoff, formatting, parser, sanitizer, config validation
    ├── integration/               # API + DB, worker + mocked Telegram, poller + mocked Telegram
    └── acceptance/                # one test module per spec §10 check (see §4 below)
```

---

## 2. Cross-cutting design notes

These apply across milestones. They pin down behavior that the spec implies but doesn't spell out.

### 2.1 Database access
- Use stdlib `sqlite3`. Each unit of work opens a short-lived connection, or takes one from a per-thread cache, with `journal_mode=WAL` (persistent, set once at init), `busy_timeout=5000`, `foreign_keys=ON`, and `synchronous=NORMAL`.
- The async code calls sync DB functions through `asyncio.to_thread`, so the event loop never blocks on SQLite.
- State transitions use `BEGIN IMMEDIATE` so that write intent is taken up front and there are no lock-upgrade deadlocks.
- **No network I/O inside a transaction** (§8). Every flow follows the pattern *commit → call network → new transaction to record the outcome*.
- `queries.py` exposes plain functions (`insert_outbound_message(conn, ...)`, `claim_due_messages(conn, now, lease_until, limit)`, and so on). Other modules contain no SQL strings.

### 2.2 Schema (v1)
- `messages`: the columns in §6, plus the extra inbound statuses from D2. Add `CHECK` constraints for `direction`, `status`, `severity`, and `kind`. Store UUIDs as TEXT. Store timestamps as TEXT in UTC ISO 8601 with a `Z` suffix.
- `idempotency_keys(source, key, request_hash, message_id REFERENCES messages(id), created_at)`, with `UNIQUE(source, key)`.
- `telegram_state(id INTEGER PRIMARY KEY CHECK (id=1), next_update_id INTEGER NOT NULL DEFAULT 0)`, seeded at init.
- `health_probe` (D9).
- Indexes: `(status, next_attempt_at)`, `(created_at)`, and `(direction, status)` for recovering inbound commands at startup.

### 2.3 Time and IDs
- Inject a `Clock` (`now()` in UTC, `monotonic()`) so that backoff, lease, and uptime tests are deterministic.
- Generate message IDs with `uuid4`, server-side only.

### 2.4 Logging
- Write one JSON object per line to stdout and to a rotating file in `/volumes/logs/`. Fields: `ts`, `level`, `event`, `request_id`/`message_id`, `source`, `command`, `transition` (for example `pending→sending`), `elapsed_ms`, `error_code`.
- A redaction filter drops any key named `authorization`, `token`, `body`, or `message`, and scrubs the bot token string from any formatted text. This matters because httpx exceptions include the Telegram URL, and that URL contains the token.
- Set the httpx and uvicorn access loggers to WARNING, or strip them, so that request URLs containing the bot token are never logged.
- Log a queue-depth and dead-count summary every 60 s and at every state change into `dead` (§9).

### 2.5 Telegram client
- Use one `httpx.AsyncClient` with `follow_redirects=False` and explicit timeouts: connect 5 s, read 10 s for `sendMessage`, and read `poll_timeout + 10 s` for `getUpdates`.
- Error classification returns one of `ok | transient | rate_limited(retry_after) | permanent(code) | auth_failed`:
  - network error, timeout, 5xx → `transient`
  - 429 → `rate_limited`, reading `parameters.retry_after`
  - 400 and 403 → `permanent` (for example `chat_not_found` or `bot_blocked`, mapped from the description to a safe code)
  - 401 and 404 → `auth_failed` (D5)
- Send only plain text: never set `parse_mode`. Set `disable_web_page_preview` (or `link_preview_options.is_disabled`).

---

## 3. Milestones

Each milestone ends in a runnable state with passing tests. The order follows §11.

### M1 — Skeleton, config, schema, queries

**Tasks**
1. Create `pyproject.toml` with the pinned dependencies (D11), a `ruff` config, and `pytest` config.
2. `config.py`: load the §4 settings plus `SWITCHBOARD_LOG_DIR`, `SWITCHBOARD_POLL_TIMEOUT` (default 30), and the producer key source (D1). Validate that:
   - the numeric IDs are integers
   - the URL is `http(s)` and has no path/query injection surface (strip the trailing slash)
   - the bind host defaults to `127.0.0.1`
   - a non-loopback bind logs a WARNING about needing a trusted network boundary

   Aggregate all errors into a single startup error that names the missing or malformed settings, never their values.
3. `schema.sql` (§2.2) and `connection.py`: create the directories, open the database, apply pragmas, and apply `schema.sql` when `user_version=0` inside one transaction, then set `user_version=1`.
4. `queries.py`: every query needed by M2–M5, written up front and unit-tested against a temp DB:
   - insert inbound and outbound rows
   - look up and insert idempotency keys
   - claim due rows with a lease
   - mark sent, retry, and dead
   - release expired leases (implicit in the claim predicate)
   - count queued and dead rows
   - read and advance `telegram_state`
   - move inbound status through its transitions
   - find unfinished inbound commands
   - the health probe update
5. `main.py`: app factory and a lifespan that currently only initializes the DB. `logging.py`: formatter and redaction.
6. `.env.example` and a README stub.

**Exit criteria:** `uv run uvicorn app.main:app` starts against an empty `/volumes`-style temp directory and creates the DB with `user_version=1` and WAL mode. Startup with a missing token fails with a readable error. Query unit tests pass.

### M2 — Producer auth, enqueue contract, idempotency, health

**Tasks**
1. `auth.py`: parse `Authorization: Bearer <t>`, then compare `t` against every configured token with `hmac.compare_digest`, without short-circuiting, to avoid timing differences. A missing, malformed, or unknown token returns `401` with `WWW-Authenticate: Bearer`.
2. `body_limit.py`: reject a request early if `Content-Length` is over 16 KiB, and enforce the same limit while reading a streamed or chunked body. Both cases return `413` (D3).
3. `models.py`: a `MessageIn` model with `extra="forbid"` and the §5 constraints:
   - `destination` must equal `Literal["owner"]`
   - `kind` must equal `Literal["event"]`
   - `topic` must match the §5 regex
   - `severity` is an enum of `info`, `warning`, `error`
   - `title` is at most 120 characters
   - `message` is 1–3000 characters
   - `correlation_id` is at most 128 characters

   Also reject all control characters in `title`, and all control characters except `\n` and `\t` in `message`. Replace FastAPI's default 422 body with a fixed, bounded error shape that doesn't echo the input values.
4. `Idempotency-Key` validation: 1–128 printable ASCII characters (`0x20`–`0x7E`), otherwise `422`. `request_hash` is the SHA-256 of the canonical JSON of the **validated** model, with sorted keys and explicit nulls.
5. Enqueue in one `BEGIN IMMEDIATE` transaction:
   1. If the key exists for this source: the same hash returns the original id and current status (D4), and a different hash returns `409`.
   2. Otherwise, insert a `messages` row with `status='pending'`, `attempts=0`, and `next_attempt_at=now`, plus the idempotency row. Commit.
   3. Catch an `IntegrityError` on the unique key, which means a concurrent duplicate, and re-read to take path 1.
   4. On `sqlite3.OperationalError` (locked past the busy timeout, disk I/O, read-only) return `503`. Never return 202 without a commit.
6. After the commit, signal the worker through an `asyncio.Event` so that delivery starts promptly.
7. `GET /health`: run the probe from D9. Return `200 {"status":"ok"}` or `503 {"status":"unavailable"}` with no other fields.
8. Per-source rate limit (D12).

**Exit criteria:** acceptance checks 1 and 2 plus the health part of check 7 pass (§4 below).

### M3 — Delivery worker and Telegram outbound

**Tasks**
1. `formatting.py`:
   - An event renders as `[{SEVERITY}] {source} — {title}\n{body}`, or `[{SEVERITY}] {source}\n{body}` when there's no title.
   - A reply renders as the body only.
   - Truncate to 4000 UTF-16 code units, since Telegram's limit is 4096 counted in UTF-16. Keep a margin and append `…` on a code-point boundary, never splitting a surrogate pair.
2. `backoff.py`: `delay(attempts, retry_after=None)` computes `base = min(5 * 2**(attempts-1), 900)` with full jitter in `[base/2, base]`. When `retry_after` is present, return `max(retry_after, jittered)`. The function is pure and table-tested.
3. `worker.py`:
   - **Claim:** in `BEGIN IMMEDIATE`, select up to N rows where `direction='outbound' AND ((status IN ('pending','retry') AND next_attempt_at <= now) OR (status='sending' AND lease_until <= now))`, ordered by `next_attempt_at, created_at`. Set `status='sending'`, `lease_until=now+60s`, and `attempts=attempts+1`, then commit.
   - **Send:** call `sendMessage` outside any transaction.
   - **Record** the outcome in a new transaction:
     - `ok` → `sent`, `telegram_chat_id`, `telegram_message_id`, `sent_at`, and clear the lease.
     - `transient`/`rate_limited` → if `attempts >= 10`, set `dead` with `last_error_code='retries_exhausted'`; otherwise set `retry` and `next_attempt_at=now+delay`.
     - `permanent` → `dead` with a safe code and an ERROR log.
     - `auth_failed` → set the row back to `retry` without counting the attempt (decrement, or don't increment for this class), then pause globally (D5).
   - **Loop:** wait on the enqueue `Event`, with a timeout equal to the next due `next_attempt_at` (capped at 5 s). Process rows sequentially; there is one worker and one destination.
   - **Rate-limit storms:** after a 429, pause the whole worker until `retry_after` has passed, not only that row. Telegram's limit is per bot, so this avoids a retry storm (§8).
   - **Shutdown:** the lifespan cancels the task. An in-flight row keeps `sending`, and its lease expires and is reclaimed on the next start. This is the documented at-least-once window.
4. `client.py`: `sendMessage` and the error classification from §2.5.

**Exit criteria:** acceptance checks 3 and 4 pass against a `respx`-mocked Telegram. A manual smoke test with the real bot delivers one event.

### M4 — Telegram polling, allowlist, command state, `/help`, `/status`

**Tasks**
1. **Startup:** call `getMe` to cache the bot username, then `deleteWebhook` with `drop_pending_updates=false` (§2). If either fails with `auth_failed`, fail startup with a clear error.
2. **Poll loop:** `getUpdates(offset=next_update_id, timeout=30, allowed_updates=["message"])`. On a transient error, back off 1 s → 60 s with jitter.
3. **Per update, strictly sequential:**
   1. Skip any `update_id < next_update_id`, which is a replay (check 7). Everything below is idempotent because the offset is advanced in the same transaction as its effects.
   2. If `message` is missing, or `chat.type != "private"`, or `from.id != USER_ID`, or `chat.id != CHAT_ID`, then in one transaction set `next_update_id = update_id + 1`. Log with a reason code and don't record or reply (D8).
   3. Parse the command (`commands.py`): take `text`, strip it, and split on whitespace. The first token must match `^/([a-z_]+)(@([A-Za-z0-9_]+))?$`.
      - If there's a suffix that isn't this bot's username (case-insensitive), ignore it.
      - Accepted forms are exactly `help` with no args, `status` with no args, and `test` with the single arg `status`.
      - Anything else starting with `/`, including extra arguments, is unknown and gets the help text.
      - Non-command text is handled per D7.
   4. **Local commands** (`/help`, `/status`, unknown): in one transaction, insert the inbound row, advance the offset, and insert the outbound reply row (`pending`). Then commit and signal the worker.
      - Inbound rows store `topic='command.<name>'` and `body=<normalized command>`, such as `/test status`, never the raw text (§6: no transcripts).
      - Local commands go straight from `received` to `handled`.
   5. `/status` builds its reply from:
      - uptime (monotonic, since lifespan start)
      - the queued count (`pending`, `retry`, `sending` outbound)
      - the dead count
      - test service reachability (`reachable`, `unreachable`, or `timeout`)

      Because reachability is a service call, `/status` follows the M5 service-call flow. Implement M5's call path early, or return `unknown` until M5 lands.
4. Command rate limit (D12).
5. Lifespan: start the poller and worker as tasks, and cancel and await them on shutdown. Close the httpx clients.

**Exit criteria:** acceptance check 5 and the local-command half of check 6 pass with mocked `getUpdates`. A manual `/help` from the owner's phone gets a reply, and a second Telegram account gets nothing.

### M5 — Test service, `/test status`, recovery, logs, end-to-end

**Tasks**
1. `test_service/main.py`: a standalone FastAPI app whose `GET /health` returns `{"status":"ok","name":"test-service"}`. It has an optional bearer check (`TEST_SERVICE_TOKEN`), no Telegram dependency, and no mutating routes (§10). For tests, env toggles can make it slow (`TEST_SERVICE_DELAY_S`) or return errors.
2. `services/test_service.py`: `GET {base_url}/health` with a 3 s total timeout, `follow_redirects=False`, and an optional `Authorization`. The URL is built only from config (§7).
   - The sanitizer reads at most 4 KiB, parses JSON, and extracts `status` and `name` only if each is a short string matching `^[A-Za-z0-9 _.-]{1,40}$`.
   - It returns one of: `ok (name)`, `degraded: <status>`, `timeout`, `unreachable`, `http_<code>`, or `invalid_response`. It never returns raw bodies, exception text, or stack traces.
3. **Service-call command flow** (§7), used by `/test status` and `/status`:
   1. Tx A: insert the inbound row with `status='processing'` and advance the offset. Commit.
   2. Make the HTTP call (no transaction).
   3. Tx B: insert the outbound reply, set inbound to `handled` (or `failed` on timeout or error, which still gets a safe reply), and commit.

   The poller doesn't fetch or process the next update until Tx B commits.
4. **Startup recovery,** before the poller starts: for each inbound row in `processing`, the command was read-only, so either re-run the call and complete Tx B, or mark it `failed` and queue "The previous request was interrupted; please retry." Recommended: re-run it once, because it's read-only, which the spec allows. Because the offset was advanced in Tx A, Telegram won't redeliver the update, so there can't be a double call.
5. Log fields and events (§9): `api.enqueued`, `delivery.sent`, `delivery.retry`, `delivery.dead`, `telegram.command`, `telegram.update_ignored`, `service.call`, plus the periodic `queue.stats`. Run a grep-based test that no log line in the test run contains the bot token, a producer token, or a message body.
6. `scripts/backup.py`: `src.backup(dst)` into a timestamped file in a configurable directory, then `PRAGMA integrity_check` on the copy.
7. `deploy/Dockerfile` and `compose.example.yml`: one uvicorn process with `--workers 1`, `/volumes` mounts, and the test service on an internal network with no published port.
8. README deployment notes:
   - single instance only
   - at-least-once delivery and the duplicate window (§8)
   - backups must use `scripts/backup.py`, never `cp` of a live WAL database
   - how to rotate credentials by editing config and restarting
   - that exposing the API beyond loopback requires a trusted network boundary and TLS

**Exit criteria:** all of §4 passes, both automated and the manual E2E run.

---

## 4. Verification matrix (spec §10)

| §10 check | Automated test (`tests/acceptance/`) | Manual E2E step |
| --- | --- | --- |
| 1. Valid submit → 202 after persist, exactly one row; wrong token, bad payload, and wrong destination rejected | `test_01_enqueue.py`: 202 and the row exists in a fresh connection, count = 1. Separate cases for 401 (missing, wrong, or malformed token), 422 (extra field, bad topic, message > 3000 characters, `destination="all"`, `kind="command"`), and 413 (17 KiB body). A supplied `source` field is rejected as an extra field. A DB made read-only or locked returns 503, and no 202 is ever returned without the row. | `curl` with a real token, then `sqlite3` to inspect the row. |
| 2. Idempotency: same key → same id, no new row; changed content → 409 | `test_02_idempotency.py`: sequential and concurrent (two threads) repeats. The same key under a different source creates a separate row, since keys are scoped per source. | `curl` twice with the same key. |
| 3. Worker delivers; DB records `sent` + message id | `test_03_delivery.py`: respx `sendMessage` returns `message_id=42`, so the row is `sent`, `telegram_message_id=42`, and `sent_at` is set. The asserted text format has no `parse_mode`. | Event arrives on the phone. |
| 4. Outage: stays queued, attempts and delay grow, resumes after recovery or restart; dead after exhaustion | `test_04_outage.py` with a fake clock: 5xx and timeouts make attempts grow and `next_attempt_at` gaps roughly double. A 429 with `retry_after=30` is honored. Recovery then gives `sent`. A restart mid-`sending` means the lease expires and the row is re-sent. Ten failures give `dead` with `retries_exhausted`. A 400 `chat not found` gives `dead` immediately. A 401 pauses without dead-lettering. | Block Telegram egress (firewall or bogus DNS), submit, watch the logs, restore it, and confirm delivery. Restart during the outage. |
| 5. Unknown user or chat: no reply, no service call; owner `/help` works | `test_05_allowlist.py`: a wrong `from.id`, a wrong `chat.id`, and a group chat each advance the offset, create no rows, and make zero calls to the respx test service route. Owner `/help` and `/help@ThisBot` get a reply row. `/help@OtherBot` is ignored. | Message the bot from a second account. |
| 6. `/status` reports state; `/test status` replies; timeout gives a short safe error | `test_06_commands.py`: the `/status` reply contains uptime, the queued count, and reachability. `/test status` with a healthy mock gives `ok (test-service)`. A delay past the timeout gives `timeout`, with the inbound row `failed` and a reply queued. A garbage JSON body gives `invalid_response`, and the raw body doesn't appear in the reply. `/test status now` and `/test foo` give the help text. | Run the test service; send the commands. Stop the test service and resend. |
| 7. Replay the same update: no duplicate call or reply; health fails when the DB is unusable | `test_07_replay_health.py`: feed the same `update_id` twice, including across a simulated restart, and check that there's exactly one inbound row, one reply row, and one service call. Crash between Tx A and Tx B, then startup recovery, gives exactly one reply. Health returns 503 when the DB file is `chmod 000`, the directory is removed, or the database is locked past the busy timeout. | `chmod` the DB file, `curl /health`, and restore it. |

Additional unit tests: backoff table, formatting and truncation (emoji and surrogate pairs at the boundary), command parser table, sanitizer table, config validation errors, and log redaction.

---

## 5. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| The bot token leaks through an httpx exception or an access log, since the token is in the URL path | A redaction filter scrubs the token string, and httpx logging is set to WARNING. The log grep test in M5 enforces this. |
| `getUpdates` sees two consumers (a stray second instance) and returns HTTP 409 | Log a clear ERROR `telegram.conflict` and back off. The README states that only one instance is supported. Optionally, take an exclusive lock file under `/volumes/data` at startup. |
| SQLite lock contention between the API threads, worker, and poller | WAL, `busy_timeout`, `BEGIN IMMEDIATE`, and short transactions with no network I/O inside them. Past the busy timeout, the API returns 503, never a false 202. |
| A slow test service blocks polling | There's a strict 3 s timeout and sequential processing is intended by the spec. Worst-case latency per command is bounded. |
| Duplicate Telegram delivery after a crash | Accepted by the spec (at-least-once) and documented in the README. The lease is kept short (60 s). |
| Telegram's 4096 limit is counted in UTF-16 after entity parsing | Truncate at 4000 UTF-16 units and send plain text only, so no entities are parsed. |
| Clock skew or jumps affect leases and backoff | Store UTC wall-clock times in the DB for durability across restarts, and use monotonic time only for uptime and in-process waits. |

---

## 6. Out of scope (per spec "Deferred")

Web dashboard, other transports, multiple users or destinations, dynamic service registration, AI or natural-language routing, scheduling, remote admin or destructive commands, confirmations, multiple workers or replicas, and any external broker. If any of these come up during implementation, add them as a follow-up spec version rather than extending the MVP.

---

## 7. Definition of done

- The M1–M5 exit criteria are met.
- `uv run pytest` is green, and `ruff check` is clean.
- All seven §10 checks pass both automatically (§4 column 2) and manually against the real Telegram bot (§4 column 3). The manual run is recorded briefly in the README or a `docs/` note.
- The README includes the at-least-once, single-instance, backup, and credential-rotation notes.
- Decisions D2 and D9 (the spec changes) are reflected back into the spec as `v0.0.1`, or explicitly rejected with this plan updated to match.

---

## 8. Implementation notes (2026-09-26)

The MVP has been implemented and all automated tests pass (unit, the §10 acceptance checks and end-to-end). The manual checks against the real bot are still open; see `TODO.md`.

The places where the implementation deviates from this plan are recorded as D13–D20 in [DECISIONS.md](DECISIONS.md), which is now the decision log for all later decisions.
