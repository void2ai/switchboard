# Podman: finishing the real run (TODO D21)

## Completed — 2026-09-28 17:24 UTC

Automated verification passed in a fresh host environment: 146 tests passed
with one harmless Starlette/AnyIO deprecation warning, and Ruff passed.

The healthy and unavailable `/test status` paths passed against the real bot.
`deploy/podman.sh install` completed, both systemd user services are active,
linger is enabled, `/health` is healthy, and the forced `podman kill
switchboard` test restarted the container. A `/status` command after that
restart succeeded and reported the test service reachable.

The host was rebooted without manually starting the containers. Both systemd
user services became active, both containers ran, `/health` returned
`{"status":"ok"}`, the current-boot logs reached `telegram.ready`, and a real
`/status` command received a reply with a fresh uptime and the test service
reachable. The Podman deployment check is complete.

The checklist below is complete. The steps are split between shell-side
checks and the Telegram or machine interactions that required the owner.

Already done: `up` with the real secret files, one real Telegram delivery,
and a reply to `/status` from the container.

Do step 1 before step 2. Stopping the test service is simpler while the
containers were started by hand than when systemd manages them.

## 1. `/test status` from the container

`/status` only shows that Switchboard itself is running. `/test status` makes
Switchboard call the test service over the internal podman network, so it is
the only check that both containers can reach each other.

**Claude**
- [x] Check that both containers run: `podman ps` shows `switchboard` and
      `switchboard-test-service`.
- [x] When you say so, stop the test service:
      `podman stop switchboard-test-service`.
- [x] Afterwards start it again: `podman start switchboard-test-service`.
- [x] Check `deploy/podman.sh logs` for errors while you test.

**You**
- [x] Send `/test status` to the bot. Expect a reply saying the test service
      is healthy.
- [x] After Claude has stopped the test service, send `/test status` again.
      Expect a quick reply saying the service is unreachable, with no hang
      and no crash.
- [x] Tell Claude what both replies said.

## 2. `deploy/podman.sh install` (systemd, restart, boot)

Right now the containers exist only because `up` was run by hand, so they
won't come back after a reboot. `install` turns the running containers into
systemd user services (so `up` must have run first). It removes the
hand-started containers, starts them again under systemd, and sets them to
restart on failure and to start at boot.

After this, stop using `up`/`down`. Use
`systemctl --user restart container-switchboard` instead, for example after
changing `container.env` or rebuilding the image.

**Claude**
- [x] Run `deploy/podman.sh install`.
- [x] If it prints the linger hint, run `loginctl enable-linger $USER`.
      Without linger the services stop when you log out and don't start at
      boot.
- [x] Check `systemctl --user status container-switchboard` and
      `container-switchboard-test-service`: both should be active.
- [x] Kill test: run `podman kill switchboard`, wait a few seconds, then check
      that `podman ps` shows it running again.
- [x] Check `/health`: `curl -s http://127.0.0.1:8080/health` if
      `SWITCHBOARD_PUBLISH` is set. Otherwise use
      `podman exec switchboard python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"`.
- [x] When everything below passes, tick the TODO item.

**You**
- [x] After the kill test, send `/status` to the bot and check that it
      answers.
- [x] Reboot the machine and don't log in to a desktop session. Then (for
      example over ssh) check that the bot answers `/status` without any
      manual start.
- [x] Tell Claude the result, so Claude can check `/health` and the logs after
      the reboot.

## If something fails

- To go back to the hand-started setup, Claude runs `deploy/podman.sh uninstall`
  and then `deploy/podman.sh up`.
- Only one Switchboard may run per bot token. Make sure no non-container
  instance (such as `python -m app`) is running at the same time.
