# AGENTS.md — working on Switchboard

These are instructions for AI coding agents and for people who change this code. Read [docs/switchboard_spec_v0.0.1_mvp.md](docs/switchboard_spec_v0.0.1_mvp.md) first. The spec is the source of truth. [docs/DECISIONS.md](docs/DECISIONS.md) is the decision log: every place where the spec is silent or where the code knowingly differs from it. When you make such a decision, add an entry there (append only, never renumber).

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # setup (Python >= 3.11)
.venv/bin/pytest -q                                          # all tests, about 2 s
.venv/bin/ruff check .                                       # lint (must be clean)
.venv/bin/python -m app                                      # run (needs env, see README)
```

Every change must leave `pytest` green and `ruff check .` clean.

## Layout

```
app/
  __main__.py          entry point: config -> logging -> uvicorn (one worker)
  main.py              create_app(), lifespan: DB init, instance lock, background tasks
  config.py            env/secret-file loading and validation (ConfigError lists all problems)
  logs.py              JSON logging, RedactionFilter, log_event()
  clock.py             Clock / FakeClock and the canonical timestamp format
  auth.py              bearer token -> source name (constant-time)
  ratelimit.py         in-memory token buckets
  state.py             AppState attached as app.state.sb
  api/messages.py      POST /api/v1/messages + enqueue_event()
  api/health.py        GET /health (real write probe)
  api/models.py        MessageIn validation, request hash, Idempotency-Key check
  db/schema.sql        ALL DDL (schema version 1)
  db/queries.py        ALL application SQL (including PRAGMAs and BEGIN/COMMIT)
  db/connection.py     Database: connect (mode=rw), transaction(), reader(), initialize()
  delivery/worker.py   claim -> send -> record loop, lease and retry handling
  delivery/backoff.py  retry_delay() (pure)
  delivery/formatting.py  plain-text rendering, UTF-16-aware truncation
  telegram/client.py   Bot API calls -> TelegramResult (never raises; classifies errors)
  telegram/poller.py   getUpdates loop, allowlist, command state machine, recovery
  telegram/commands.py parser + reply texts
  services/test_service.py  fixed-URL health client + response sanitizer
test_service/main.py   standalone test service (GET /health only)
scripts/backup.py      WAL-aware backup
tests/unit, tests/acceptance (one file per spec §10 check), tests/integration
```

## Invariants (don't break these)

1. **All SQL lives in `app/db/queries.py`, and all DDL in `app/db/schema.sql`.** Other modules call query functions. Tests may use raw SQL for setup and inspection.
2. **No network I/O inside a database transaction.** The worker uses claim (transaction), then send (no transaction), then record (transaction). Service commands use Tx A, then the HTTP call, then Tx B.
3. **`202` only after commit.** A database error returns `503`, never a false `202`.
4. **The Telegram offset moves only inside the transaction that records the update's effects.** Replaying an update must never run a command twice. There are three layers of protection, each with its own test: the early offset check, the re-check inside the transaction, and the unique `telegram_update_id` index.
5. **Authorization uses numeric IDs only.** A message must have `from.id == TELEGRAM_USER_ID`, `chat.id == TELEGRAM_CHAT_ID` and `chat.type == "private"`. Never use usernames. Unauthorized updates are not recorded and never answered.
6. **Nothing from Telegram text reaches a URL.** The test service URL is config plus the fixed route `/health`. Redirects are never followed.
7. **Plain text only when sending.** Never set `parse_mode`. Replies and events are truncated to 4000 UTF-16 units.
8. **Never log secrets or message bodies.** Use `log_event(log, level, "area.event", **safe_fields)`. Field names such as `body`, `message`, `text`, `token` and `authorization` are dropped automatically, but don't rely on that. For exceptions, log `error_code=type(exc).__name__`, never `str(exc)`, because httpx exception text contains the URL, and that URL contains the bot token.
9. **Raw chat text is never stored.** Inbound rows keep only the normalized command (for example `/test status`), or `NULL` for ignored text.
10. **Exactly one process.** Keep `workers=1`. The lock file enforces this at startup.
11. **Background loops must not die.** `DeliveryWorker.run` and `TelegramPoller.run` catch every `Exception`, log it and back off. Only `CancelledError` ends them.
12. **Time comes from `Clock`.** Don't call `datetime.now()` or `time.monotonic()` directly in logic that tests need to control. Stored timestamps use `format_ts` (fixed width, so string comparison is time comparison).

## Conventions

- Target Python 3.11+ (the plan said 3.12; 3.11 is the minimum actually used) and write `from __future__ import annotations`.
- Sync SQLite code runs in threads via `asyncio.to_thread`. Each unit of work opens its own connection, which is cheap and avoids sharing connections across threads.
- Comments explain *why*: the invariant or spec section involved. Match the existing density.
- New schema changes go in `app/db/migrations/NNN_description.sql`. Then bump `SCHEMA_VERSION` in `db/connection.py`, and add any new queries to `queries.py`.
- Every spec §10 check has a test in `tests/acceptance/`. When a behavior changes, update the matching test.
- Tests mock HTTP with `respx`, and time with `FakeClock`. No test touches the real network.

## Things to know

- `TelegramClient` methods never raise. Branch on `result.outcome` (`ok | transient | rate_limited | permanent | auth_failed`).
- A 401 or 404 from Telegram means the bot token is wrong. The worker pauses and does **not** dead-letter messages or count the attempt.
- The poller starts by calling `getMe` (to learn the bot username for `/cmd@bot`) and `deleteWebhook`. It retries forever, so a Telegram outage never blocks the HTTP API.
- `create_app(settings, start_background=False)` serves the API without the worker, poller or lock. The API tests use this.
