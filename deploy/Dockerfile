# Switchboard container image. Runs exactly one process (spec §2: no multiple
# workers or replicas). Build from the repository root:
#   podman build -f deploy/Dockerfile -t switchboard .    (docker works too)
# Fully qualified base image: podman does not resolve short names unless
# unqualified-search-registries is configured (D21).
FROM docker.io/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SWITCHBOARD_DATABASE_PATH=/volumes/data/switchboard.sqlite3 \
    SWITCHBOARD_LOG_DIR=/volumes/logs

WORKDIR /srv/switchboard
COPY pyproject.toml README.md ./
COPY app ./app
COPY test_service ./test_service
COPY scripts ./scripts
RUN pip install --no-cache-dir . \
    && groupadd --system --gid 10001 switchboard \
    && useradd --system --uid 10001 --gid 10001 --home /srv/switchboard switchboard \
    && mkdir -p /volumes/data /volumes/config /volumes/logs \
    && chown -R switchboard /volumes

USER switchboard
VOLUME ["/volumes/data", "/volumes/config", "/volumes/logs"]
EXPOSE 8080

# Inside a container, bind beyond loopback only on a private network
# (see compose.example.yml) — never publish the port to the internet.
CMD ["python", "-m", "app"]
