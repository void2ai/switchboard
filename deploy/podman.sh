#!/usr/bin/env bash
# Run Switchboard + test service with rootless podman (D21). Plain podman
# instead of podman-compose: Debian 12's podman-compose 1.0.3 ignores
# userns_mode, and systemd units are the only way rootless containers come
# back after a reboot.
#
#   deploy/podman.sh build       build the image (localhost/switchboard)
#   deploy/podman.sh up          create networks, start both containers
#   deploy/podman.sh down        stop and remove both containers
#   deploy/podman.sh logs        follow switchboard's logs
#   deploy/podman.sh install     hand the containers to systemd --user (start at boot)
#   deploy/podman.sh uninstall   remove the systemd units and the containers
#
# After "install": edit container.env or rebuild the image, then
# "systemctl --user restart container-switchboard" (units recreate the
# container on every start). The volumes path and SWITCHBOARD_PUBLISH are
# baked into the units: change them with uninstall, up, install.
#
# Needs in $VOLUMES/config (default: the repository's volumes/, git-ignored):
#   bot_token        Telegram bot token (mode 0600)
#   producers.json   {"research": "<long random token>", ...} (mode 0600)
#   container.env    non-secret settings, see deploy/container.env.example
#
# Only ONE switchboard may run per bot token and database: stop any
# non-container instance first (two pollers fight over getUpdates, and the
# lock file refuses a second instance on the same database).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
VOLUMES=${SWITCHBOARD_VOLUMES:-$REPO/volumes}
IMAGE=localhost/switchboard:latest
SB=switchboard
TS=switchboard-test-service
NET_INTERNAL=switchboard-internal
NET_EGRESS=switchboard-egress
# Optional host address to publish the API on, e.g. 127.0.0.1:8080. Keep it
# on loopback (README "Network exposure"). Empty = not published.
PUBLISH=${SWITCHBOARD_PUBLISH:-}
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user

die() { echo "podman.sh: $*" >&2; exit 1; }

check_config() {
    local f
    for f in bot_token producers.json container.env; do
        [[ -r $VOLUMES/config/$f ]] || die "missing $VOLUMES/config/$f (see header of $0)"
    done
    mkdir -p "$VOLUMES/data" "$VOLUMES/logs"
}

ensure_networks() {
    # internal: no route out, switchboard <-> test service only.
    podman network exists "$NET_INTERNAL" || podman network create --internal "$NET_INTERNAL" >/dev/null
    # egress: normal bridge so switchboard can reach api.telegram.org.
    podman network exists "$NET_EGRESS" || podman network create "$NET_EGRESS" >/dev/null
}

cmd_build() {
    podman build -f "$REPO/deploy/Dockerfile" -t "$IMAGE" "$REPO"
}

cmd_up() {
    # --replace would swap the systemd-managed container behind systemd's back.
    systemctl --user -q is-enabled "container-$SB.service" 2>/dev/null \
        && die "installed as a systemd service: use 'systemctl --user restart container-$SB', or '$0 uninstall' first"
    check_config
    podman image exists "$IMAGE" || cmd_build
    ensure_networks
    local publish=()
    [[ -n $PUBLISH ]] && publish=(--publish "$PUBLISH:8080")

    # --init: python is not PID 1, so a SIGTERM that arrives before uvicorn
    # installs its handlers still stops the container instead of hanging 10 s.
    podman run -d --replace --init --name "$TS" \
        --network "$NET_INTERNAL" --network-alias test-service \
        "$IMAGE" uvicorn test_service.main:app --host 0.0.0.0 --port 8081 >/dev/null

    # keep-id maps your host user to the image's uid/gid 10001: the container
    # can read your 0600 secret files, and what it writes stays owned by you.
    podman run -d --replace --init --name "$SB" \
        --userns=keep-id:uid=10001,gid=10001 \
        --network "$NET_INTERNAL,$NET_EGRESS" \
        "${publish[@]}" \
        --env-file "$VOLUMES/config/container.env" \
        -e SWITCHBOARD_BOT_TOKEN_FILE=/volumes/config/bot_token \
        -e SWITCHBOARD_PRODUCER_KEYS_FILE=/volumes/config/producers.json \
        -e SWITCHBOARD_TEST_SERVICE_URL=http://test-service:8081 \
        -e SWITCHBOARD_BIND_HOST=0.0.0.0 \
        -e SWITCHBOARD_BIND_PORT=8080 \
        -v "$VOLUMES/data:/volumes/data" \
        -v "$VOLUMES/config:/volumes/config:ro" \
        -v "$VOLUMES/logs:/volumes/logs" \
        "$IMAGE" >/dev/null
    podman ps --filter "name=^($SB|$TS)\$" --format '{{.Names}}\t{{.Status}}'
}

cmd_down() {
    podman rm -f --ignore "$SB" "$TS" >/dev/null
}

cmd_install() {
    # Units are generated from the running containers (--new: systemd
    # recreates them on every start), so run "up" first.
    podman container exists "$SB" && podman container exists "$TS" \
        || die "run '$0 up' first"
    mkdir -p "$UNIT_DIR"
    local c
    for c in "$SB" "$TS"; do   # podman 4.3 takes one container per call
        (cd "$UNIT_DIR" && podman generate systemd --new --name --files --restart-policy=on-failure "$c" >/dev/null)
    done
    cmd_down
    systemctl --user daemon-reload
    systemctl --user enable --now "container-$TS.service" "container-$SB.service"
    # Without linger, user services stop at logout and don't start at boot.
    [[ $(loginctl show-user "$USER" -p Linger --value 2>/dev/null) == yes ]] \
        || echo "podman.sh: run 'loginctl enable-linger $USER' so this starts at boot" >&2
}

cmd_uninstall() {
    systemctl --user disable --now "container-$SB.service" "container-$TS.service" 2>/dev/null || true
    rm -f "$UNIT_DIR/container-$SB.service" "$UNIT_DIR/container-$TS.service"
    systemctl --user daemon-reload
    cmd_down
}

case ${1:-} in
    build) cmd_build ;;
    up) cmd_up ;;
    down) cmd_down ;;
    logs) podman logs -f "$SB" ;;
    install) cmd_install ;;
    uninstall) cmd_uninstall ;;
    *) die "usage: $0 build|up|down|logs|install|uninstall" ;;
esac
