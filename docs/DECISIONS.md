# Switchboard — decision log

The spec ([switchboard_spec_v0.0.1_mvp.md](switchboard_spec_v0.0.1_mvp.md)) is the source of truth. This file records every decision made where the spec is silent or ambiguous, and every place the code knowingly differs from the spec or the plan.

## How to add a decision

- Add new entries at the end. Never renumber. Numbers are referenced from code comments, tests and other docs.
- Don't rewrite an old decision. Add a new one and mark the old one `superseded by Dn`.
- Keep each entry short: what was decided, why, and which spec section it covers (§N).
- **Spec change** marks a decision that adds something the spec doesn't list. Those need to be folded into the next spec version, or rejected.
- Status is one of `accepted`, `superseded by Dn`, `rejected`.

---

## D1–D12: MVP plan decisions (2026-09-26)

Made before implementation. The full text, with the alternatives considered, is in [plan §0](switchboard_spec_v0.0.0_mvp_plan.md#0-decisions-to-confirm-before-starting). All were implemented.

| # | Topic | Decision | Status |
| --- | --- | --- | --- |
| D1 | Producer keys (§4) | JSON file `{"source": "token"}` via `SWITCHBOARD_PRODUCER_KEYS_FILE`, or inline for dev. Tokens at least 32 chars and unique; source names validated. | accepted |
| D2 | Inbound command state (§7) | **Spec change:** inbound statuses `processing`, `handled`, `failed`. | accepted; spec fold-in open (TODO.md) |
| D3 | Error codes (§5) | Oversized body `413`; malformed JSON or schema violation `422`. | accepted |
| D4 | Idempotent repeat (§5) | `202` with the original `id` and current status. No new row. | accepted |
| D5 | Telegram `401`/`404` | Bad bot token: pause and retry at max backoff. Don't dead-letter, don't count the attempt. | accepted |
| D6 | Command replies | Outbound rows with `kind='reply'` and `correlation_id` = inbound id. Sent without the event header. | accepted |
| D7 | Owner plain text | Recorded as `ignored` with no body. No reply. | accepted |
| D8 | Unauthorized updates (§7) | No row, offset advanced, logged without user text or username. | accepted |
| D9 | Health probe (§5) | **Spec change:** singleton `health_probe` table, so `/health` does a real write. | accepted; spec fold-in open (TODO.md) |
| D10 | Schema versioning | `PRAGMA user_version`, migrations in `app/db/migrations/NNN_*.sql`. | accepted |
| D11 | Python and tooling | Python 3.12 with `uv`. | superseded by D13 |
| D12 | Rate limits (§9) | 60 req/min per source (burst 20) → `429`; 20 commands/min per owner. | accepted |

---

## D13: Python 3.11 and pip instead of 3.12 and uv

- **Date:** 2026-09-26
- **Status:** accepted (supersedes D11)
- **Decision:** target Python ≥ 3.11, installed with pip into a venv.
- **Why:** the development host has Python 3.11 and no `uv`. A `uv` lockfile is still in TODO.md.

## D14: Body limit enforced in the endpoint

- **Date:** 2026-09-26
- **Status:** accepted
- **Spec:** §5
- **Decision:** there's no separate body-limit middleware. `api/messages.py` enforces the 16 KiB limit itself, checking both `Content-Length` and the streamed byte count.
- **Why:** a single place to check both, and a client can't get around the limit by sending a false `Content-Length`.

## D15: `415` for a non-JSON Content-Type

- **Date:** 2026-09-26
- **Status:** accepted
- **Spec:** §5 (not specified)
- **Decision:** a request with a `Content-Type` other than JSON gets `415 Unsupported Media Type`.

## D16: Telegram failures at startup don't block the app

- **Date:** 2026-09-26
- **Status:** accepted
- **Spec:** §8 (the queue survives a Telegram outage)
- **Decision:** if `getMe` or `deleteWebhook` fails at startup (including `auth_failed`), startup continues. The poller retries in the background, and the HTTP API keeps accepting and queueing events.
- **Why:** services shouldn't lose the ability to queue events because Telegram is down.

## D17: Recovery of interrupted commands runs inside the poll loop

- **Date:** 2026-09-26
- **Status:** accepted
- **Spec:** §7
- **Decision:** recovery of `processing` commands runs inside the poller's guarded loop, and runs again after any unexpected poll-loop error.
- **Why:** an interrupted command is finished without waiting for a restart.

## D18: Single-instance lock file

- **Date:** 2026-09-26
- **Status:** accepted
- **Spec:** §5 (listed as optional)
- **Decision:** an exclusive `flock` on `<database>.lock` makes a second instance fail at startup.

## D19: Database opened with `mode=rw`

- **Date:** 2026-09-26
- **Status:** accepted
- **Decision:** connections open with `mode=rw`, never `rwc`.
- **Why:** if the database file is deleted, `/health` and the API return `503` rather than silently creating an empty database.

## D20: Control characters in producer fields

- **Date:** 2026-09-26
- **Status:** accepted
- **Spec:** §5
- **Decision:** `title` and `correlation_id` reject all C0 and C1 control characters and bidirectional override characters. `message` also allows tab, newline and carriage return. Other Unicode format characters are allowed, such as the zero-width joiner used in emoji.

## D21: Rootless podman is the deployment target

- **Date:** 2026-09-27
- **Status:** accepted
- **Spec:** silent (§2 only requires one process)
- **Decision:** production runs under rootless podman on the host (Debian 12, podman 4.3), driven by `deploy/podman.sh` with plain `podman` commands, not podman-compose. The script runs switchboard with `--userns=keep-id:uid=10001,gid=10001`, so the image's user maps to the host user who owns `volumes/`: the 0600 secret files stay readable, and the database and logs stay owned by that user. `install` generates systemd user units (`podman generate systemd --new`), which restart the container on failure and start it at boot (needs linger). Both containers run with `--init`. `deploy/Dockerfile` names its base image fully (`docker.io/library/...`) and stays buildable with Docker; CI still uses Docker. `deploy/compose.example.yml` stays as the Docker example.
- **Why:** podman-compose 1.0.3 (Debian 12) silently ignores `userns_mode`, so the container can't read the secret files; rootless podman has no daemon, so `restart:` alone doesn't survive a reboot; podman here doesn't resolve short image names. Without `--init`, a SIGTERM that arrives before uvicorn installs its handlers is ignored by PID 1 and the stop hangs for 10 s.

## D22: MVP specification v0.0.1 folds in D2 and D9

- **Date:** 2026-09-28
- **Status:** accepted
- **Decision:** publish `switchboard_spec_v0.0.1_mvp.md` as the source of
  truth, explicitly adding the inbound `processing`, `handled`, and `failed`
  states from D2 and the writable singleton `health_probe` table from D9.
- **Why:** both decisions are implemented and verified; keeping them only in
  the plan and decision log left the source-of-truth specification incomplete.
