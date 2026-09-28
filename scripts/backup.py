"""WAL-aware online backup of the Switchboard database (spec §2).

Never copy a live WAL database with ``cp``: the ``-wal`` file may hold
committed transactions not yet in the main file. This script uses SQLite's
backup API, which produces a consistent snapshot while Switchboard runs, then
verifies the copy with ``PRAGMA integrity_check``.

Usage:
    python scripts/backup.py [--db PATH] [--out-dir DIR]

Defaults: ``$SWITCHBOARD_DATABASE_PATH`` (or /volumes/data/switchboard.sqlite3)
and ``/volumes/data/backups``.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# Allow running as a plain script from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_DATABASE_PATH  # noqa: E402
from app.db import queries  # noqa: E402


def backup(db_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"{db_path.stem}-{stamp}.sqlite3"

    source = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        source.close()
    try:
        result = queries.integrity_check(destination)
    finally:
        destination.close()
    if result != "ok":
        raise RuntimeError(f"integrity check failed on {target}: {result}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=Path(
        os.environ.get("SWITCHBOARD_DATABASE_PATH") or DEFAULT_DATABASE_PATH))
    parser.add_argument("--out-dir", type=Path, default=Path("/volumes/data/backups"))
    args = parser.parse_args()
    if not args.db.exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 1
    target = backup(args.db, args.out_dir)
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
