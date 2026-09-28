"""Entry point: ``python -m app``.

Loads and validates configuration, sets up logging and runs a single
uvicorn worker (the MVP must never run more than one process).
"""

from __future__ import annotations

import sys

import uvicorn

from app.config import ConfigError, load_settings
from app.logs import configure_logging
from app.main import create_app


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"switchboard: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.secrets(), settings.log_dir)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        workers=1,
        access_log=False,  # request lines add nothing the app logs lack
        log_config=None,  # keep our JSON handlers (with redaction) on the root logger
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
