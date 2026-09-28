# TODO

The MVP code is complete and the automated tests pass. What remains is listed below, in rough priority order.

## Security

- [x] Revoke the bot token exposed during private, pre-release development.
- [x] Publish v0.0.1 from a clean snapshot that excludes the affected private
      history. Runtime secrets remain in ignored local files or protected
      secret files under `volumes/config/`.

## Before calling the MVP done (spec §10, manual part)

- [ ] **Manual end-to-end run against the real Telegram bot.** Follow the manual column of the verification matrix in the plan (§4), and record the results briefly in `docs/`.
  - [x] An event arrives on the phone. Check that the row is `sent` and `telegram_message_id` is set.
  - [x] Outage: block egress to api.telegram.org, submit an event, watch `delivery.retry` in the logs, restore egress and confirm delivery. Restart the process during the outage.
  - [ ] Message the bot from a second Telegram account. It must get no reply.
  - [x] Owner sends `/help`, `/status` and `/test status`. Stop the test service and repeat `/test status`.
  - [x] Make the DB file unreadable and check that `curl /health` returns 503.
- [x] **Run the container for real (podman, D21).** Real delivery, `/status`, healthy and unavailable `/test status`, systemd installation, forced-crash recovery, `/health`, and automatic startup after reboot all passed. Evidence is recorded in `docs/podman_real_run_checklist.md`.
- [x] **Spec follow-up:** v0.0.1 folds plan decisions D2 (inbound statuses `processing`, `handled`, `failed`) and D9 (the `health_probe` table) into the source-of-truth spec.

## Known limitations / small improvements

- [ ] **Rate limiting for unauthenticated clients.** Rate limits are per authenticated source only. Repeated 401s aren't throttled. This is low risk on loopback, but add a per-IP limit before binding beyond loopback.
- [ ] **Data retention.** `messages` and `idempotency_keys` grow forever. Add a pruning job, for example for `sent` or `ignored` rows older than N days, and delete their idempotency keys in the same transaction, as §5 requires ("retain keys as long as message records remain").
- [ ] **Stale-rate-limit notice state.** The poller's "slow down" notice window lives in memory and resets on restart. That's harmless, but worth knowing.
- [x] **HTTPS check.** `SWITCHBOARD_TELEGRAM_API_BASE` is fully validated (host, port, no query, fragment or credentials), and a non-https base logs `config.insecure_telegram_api_base` at startup.
- [ ] **Stricter Content-Length handling.** A malformed `Content-Length` currently returns 413. A 400 would be more precise.
- [ ] **Health endpoint when not on loopback.** It is always unauthenticated. It exposes nothing, but consider restricting it to loopback or the internal network if the API is bound more widely.
- [ ] **Dead-letter tooling.** Add a small CLI to list dead letters and requeue them (`dead` → `pending`, `attempts = 0`). Today this is done by hand in SQL (see README).
- [x] **CI.** `.github/workflows/ci.yml` runs ruff and pytest (Python 3.11 and 3.12), builds the image, and smoke-tests it (`/health` 200, one event 202). First green run: CI #1 on `8ab0ce6` (2026-09-27).
- [ ] **`uv` lockfile.** The plan (D11) proposed `uv`. The project currently uses pip with a venv and version ranges in `pyproject.toml`. Add a lockfile for reproducible builds.
- [ ] **Starlette TestClient deprecation warning** (`httpx` → `httpx2`). Revisit when FastAPI or Starlette settle on it.

## Deferred by the spec (not MVP)

These are out of scope for the MVP: a web dashboard, other transports, multiple users or destinations, dynamic service registration, AI or natural-language routing, a scheduler, remote administration, destructive commands, confirmations, multiple workers or replicas, and external brokers. Add real services one at a time after the manual MVP checks pass.
