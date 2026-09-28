# MVP verification — 2026-09-28

This file records manual and host-level verification against the real Telegram
bot and the rootless Podman deployment. Automated tests use mocked HTTP and do
not contact Telegram.

## Automated verification

- Fresh Python 3.11 virtual environment on the host.
- `pytest -q`: 146 passed in 1.93 seconds.
- One non-failing third-party deprecation warning from Starlette's TestClient
  referring to the AnyIO `BlockingPortal` alias.
- `ruff check .`: all checks passed.

## Real Podman deployment

- A real outbound event was delivered to Telegram before this verification;
  the database recorded it as sent.
- `/status` replied from the hand-started container.
- `/help` replied with the complete supported-command list after the reboot.
- `/test status` replied `Test service: ok (test-service)`.
- With `switchboard-test-service` stopped, `/test status` replied
  `Test service: unreachable` promptly; Switchboard did not hang or crash.
- `deploy/podman.sh install` installed and enabled both systemd user services.
- User lingering was already enabled.
- Killing the `switchboard` container caused systemd to recreate it. `/health`
  returned `{"status":"ok"}`, the logs reached `telegram.ready`, and a real
  `/status` command succeeded after restart.
- After a host reboot, without a manual container start, both systemd services
  and containers were running. `/health` returned `{"status":"ok"}`, startup
  logs reached `telegram.ready`, and a real `/status` command replied with a
  fresh uptime and `Test service: reachable (ok (test-service))`.
- With the database file temporarily set to mode `000`, `/health` returned
  `503 {"status":"unavailable"}`. A shell trap restored the original `0644`
  mode, after which `/health` returned `200 {"status":"ok"}`.
- With only the Switchboard egress network disconnected, event
  `f17a965e-0332-4eee-9c45-de770a1f503a` was accepted with HTTP 202 and logged
  `delivery.retry` delays of 3.3, 7.4, 18.2, and 29.9 seconds. Switchboard was
  restarted while still isolated; after restart the queue contained one retry
  row and delivery continued failing safely. Reconnecting egress delivered the
  same event on attempt 5. Its row became `sent` with Telegram message ID 43
  and a `sent_at` timestamp, and receipt was confirmed on the owner's phone.

## Still pending from the full manual §10 matrix

- Send a message from a second Telegram account and confirm there is no reply.
  This remains pending because the owner does not currently have access to a
  second Telegram account.
