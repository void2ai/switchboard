# Switchboard

An open-source **void2.ai** project.

Switchboard is a small daemon that connects your own services to **one human through Telegram**.

- **Outbound:** a service sends an authenticated `POST` to a local HTTP API. Switchboard stores the event in SQLite, answers `202`, and a background worker delivers it to your private Telegram chat, retrying through outages.
- **Inbound:** you, the single allowlisted Telegram user, can send `/help`, `/status` and `/test status`. Switchboard answers them itself or asks a registered test service.

Producers never see the bot token or chat IDs. Telegram is only an adapter.

This is the MVP described in [docs/switchboard_spec_v0.0.1_mvp.md](docs/switchboard_spec_v0.0.1_mvp.md). The implementation plan is in [docs/switchboard_spec_v0.0.0_mvp_plan.md](docs/switchboard_spec_v0.0.0_mvp_plan.md), and design decisions are logged in [docs/DECISIONS.md](docs/DECISIONS.md).

## Quick start (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp volumes/config/.env.example volumes/config/.env   # then edit: bot token, your user/chat id, producer tokens
set -a; . ./volumes/config/.env; set +a
# JSON values must be single-quoted in .env, e.g. SWITCHBOARD_PRODUCER_KEYS='{"research":"..."}'

# Terminal 1: the test service
.venv/bin/uvicorn test_service.main:app --host 127.0.0.1 --port 8081

# Terminal 2: Switchboard (always a single process)
.venv/bin/python -m app
```

Send an event:

```bash
curl -i http://127.0.0.1:8080/api/v1/messages \
  -H "Authorization: Bearer <producer token>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: job-482-done" \
  -d '{"destination":"owner","kind":"event","topic":"research.completed",
       "severity":"info","title":"Research complete","message":"Found 6 useful papers."}'
# HTTP/1.1 202 Accepted
# {"id":"…","status":"pending"}
```

In Telegram, send `/help`, `/status` or `/test status` to your bot.

To find your numeric user ID, message the bot once and then open `https://api.telegram.org/bot<token>/getUpdates` while Switchboard is **not** running. Read `message.from.id`. In a private chat, `chat.id` is the same number.

## Configuration

Settings are read from environment variables. Any secret can instead be read from a file: set `<NAME>_FILE` to the file's path, and the file takes precedence. Startup fails with a list of the missing or malformed settings. The message names the settings and never prints their values.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `SWITCHBOARD_BOT_TOKEN` / `_FILE` | yes | – | Telegram bot token |
| `SWITCHBOARD_TELEGRAM_USER_ID` | yes | – | Numeric ID of the only authorized sender |
| `SWITCHBOARD_TELEGRAM_CHAT_ID` | yes | – | Private chat ID; also the outbound destination |
| `SWITCHBOARD_PRODUCER_KEYS` / `_FILE` | yes | – | JSON `{"source": "token"}`. Each token must be at least 32 characters and distinct. Source names match `[a-z][a-z0-9_-]{0,63}`, and `switchboard` is reserved. |
| `SWITCHBOARD_TEST_SERVICE_URL` | no | – | Base URL of the test service. Only `<url>/health` is ever called. |
| `SWITCHBOARD_TEST_SERVICE_TOKEN` / `_FILE` | no | – | Bearer credential sent to the test service |
| `SWITCHBOARD_BIND_HOST` / `_PORT` | no | `127.0.0.1` / `8080` | HTTP listener |
| `SWITCHBOARD_DATABASE_PATH` | no | `/volumes/data/switchboard.sqlite3` | SQLite file |
| `SWITCHBOARD_LOG_DIR` | no | `/volumes/logs` | Rotating JSON log file. Set it to an empty string for stdout only. |
| `SWITCHBOARD_POLL_TIMEOUT` | no | `30` | Telegram long-poll seconds (1–50) |
| `SWITCHBOARD_TELEGRAM_API_BASE` | no | `https://api.telegram.org` | Only for tests or a local Bot API server |

To rotate a credential, replace the value or file and restart.

## HTTP API

### `POST /api/v1/messages`

| Field | Rules |
| --- | --- |
| `destination` | required, must be `"owner"` |
| `kind` | required, must be `"event"` |
| `topic` | required, matches `^[a-z][a-z0-9_.-]{0,79}$` |
| `severity` | required: `info`, `warning` or `error` |
| `title` | optional, at most 120 characters |
| `message` | required plain text, 1–3000 characters, not blank. Tab and newline are the only control characters allowed. |
| `correlation_id` | optional, at most 128 characters |

The source is taken from the bearer token, never from the body. Unknown fields are rejected.

| Status | When |
| --- | --- |
| `202` `{"id","status"}` | The message was committed to SQLite. This means it's durably queued, not that it has been delivered. |
| `401` | Missing or invalid bearer token |
| `409` | The `Idempotency-Key` was already used by this source for different content |
| `413` | The body is larger than 16 KiB |
| `415` | The `Content-Type` isn't `application/json` |
| `422` | The JSON or schema is invalid, or the `Idempotency-Key` is malformed. Error details name fields and error types only; input values are never echoed back. |
| `429` | Rate limit exceeded: 60 requests per minute per source, burst 20. The response includes `Retry-After`. |
| `503` | The database is unavailable. Nothing was queued, so retry later. |

`Idempotency-Key` is optional and must be 1–128 printable ASCII characters. Repeating a key with the same content returns `202` with the original `id` and that message's **current** status, and creates nothing new. Keys are scoped per source.

### `GET /health`

Returns `200 {"status":"ok"}` only when SQLite completes a real read/write probe, and `503 {"status":"unavailable"}` otherwise. It needs no token and exposes no data. Telegram's reachability is reported in the logs, not here.

## Telegram commands

| Command | Reply |
| --- | --- |
| `/help` | The list of commands |
| `/status` | Uptime, queued count, dead-letter count, test service reachability |
| `/test status` | Summary of the test service's `GET /health`, for example `ok (test-service)`, `timed out` or `unreachable` |

A `@YourBot` suffix is accepted, for example `/help@YourBot`. Any other `/command`, including a known command with extra arguments, is answered with the help text. Plain text and media from you are recorded as `ignored`, without their content, and get no reply. Messages from any other user or chat, or from group chats, get no reply and are never recorded.

## Operations

### Delivery semantics: at least once

A message can be delivered twice. This happens if Switchboard crashes after Telegram accepted a message but before the database recorded it as `sent`. After the 60-second lease expires, the message is sent again. This tradeoff is deliberate: the only alternative would be to sometimes lose messages.

### Retries and dead letters

- **Retried:** network errors, timeouts, HTTP 5xx and Telegram 429. The first retry waits 5 s, and the delay doubles each time up to a maximum of 15 minutes, with jitter. Telegram's `retry_after` is honored, and a 429 pauses all sending.
- **Dead after retries:** a message still failing after 10 attempts becomes `dead` with `last_error_code = retries_exhausted`.
- **Dead immediately:** permanent errors, such as a missing chat, a blocked bot or a malformed request.
- **Bot token rejected (401 or 404):** this stops all sending for 15 minutes at a time. Messages are **not** dead-lettered and stay queued. Fix the token and restart.

To inspect dead letters:

```bash
sqlite3 /volumes/data/switchboard.sqlite3 \
  "SELECT id, source, topic, attempts, last_error_code, created_at FROM messages WHERE status='dead'"
```

Queue depth and the dead-letter count are logged every 60 s as the `queue.stats` event.

### Single instance

Run exactly one Switchboard process per database. It owns the SQLite queue and the Telegram polling loop. A lock file (`<database>.lock`) makes a second instance fail at startup. Don't run several uvicorn workers or container replicas.

### Backups

Don't copy a live WAL database with `cp`. Use the backup script, which uses SQLite's backup API and verifies the copy:

```bash
python scripts/backup.py --db /volumes/data/switchboard.sqlite3 --out-dir /volumes/data/backups
```

### Network exposure

The API binds to loopback by default. If you bind it elsewhere, for example inside a container network, keep it behind a trusted network boundary and use TLS across any untrusted hop. Switchboard logs a warning at startup when it isn't bound to loopback. Never expose the API to the public internet.

### Logs

Logs are JSON lines on stdout and in `<LOG_DIR>/switchboard.log`, which rotates at 10 MB with 5 files kept. Bot tokens, producer tokens, `Authorization` headers and message bodies are never logged. The redaction filter also scrubs the bot token from exception text, because the token is part of every Telegram API URL.

### Containers (rootless podman)

`deploy/podman.sh` runs Switchboard and the test service with rootless podman, using plain `podman` commands rather than podman-compose (see D21 in [docs/DECISIONS.md](docs/DECISIONS.md)). Put the configuration in the repository's git-ignored `volumes/config/`:

```bash
# secrets as files, readable only by you
install -m 600 /dev/null volumes/config/bot_token        # then write the bot token into it
install -m 600 /dev/null volumes/config/producers.json   # {"research": "<long random token>"}
cp deploy/container.env.example volumes/config/container.env   # then set your user/chat id

deploy/podman.sh up          # build if needed, create networks, start both containers
deploy/podman.sh logs        # follow the logs (Ctrl+C to stop following)
deploy/podman.sh install     # hand over to systemd --user: restart on failure, start at boot
```

`container.env` is read by `podman --env-file`: write `KEY=VALUE` with no quotes. The container's user (UID 10001) is mapped to your own user, so it can read your 0600 files, and the database and logs it writes stay owned by you. The API isn't published on the host. To reach it from host-local producers, set `SWITCHBOARD_PUBLISH=127.0.0.1:8080` when running `up`. After `install`, apply config changes or a rebuilt image with `systemctl --user restart container-switchboard`. `deploy/podman.sh uninstall` removes the units and the containers. Stop any development instance that uses the same bot token first.

`deploy/compose.example.yml` is a Docker example. CI builds `deploy/Dockerfile` with Docker and smoke-tests it.

## Development

```bash
.venv/bin/pytest            # unit, acceptance (spec §10) and end-to-end tests
.venv/bin/ruff check .
```

- `tests/acceptance/test_0N_*.py` maps one-to-one to the checks in §10 of the spec.
- `tests/integration/test_end_to_end.py` runs the whole app, with the Telegram API and the test service mocked at the HTTP layer. It also checks that no secret or message body reaches the log file.

See [AGENTS.md](AGENTS.md) for the code layout and invariants, and [TODO.md](TODO.md) for open work.

## License

Licensed under the [Apache License 2.0](LICENSE).
