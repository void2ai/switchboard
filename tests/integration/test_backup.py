"""WAL-aware backup script produces a consistent, integrity-checked copy."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from app.api.messages import enqueue_event
from app.api.models import MessageIn
from app.clock import FakeClock
from app.db.connection import Database
from tests.conftest import valid_event

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup.py"


def _load_backup_module():
    spec = importlib.util.spec_from_file_location("backup_script", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_backup_includes_uncheckpointed_wal_data(db: Database, clock: FakeClock,
                                                 tmp_path: Path) -> None:
    enqueue_event(db, clock, "research", MessageIn.model_validate(valid_event()), None)
    # The row may still live only in the -wal file; the backup must include it.
    target = _load_backup_module().backup(db.path, tmp_path / "backups")
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()
