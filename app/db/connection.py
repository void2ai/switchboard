"""SQLite connection handling and schema migration.

Each unit of work opens its own short-lived connection. SQLite connections
are cheap, this keeps thread-safety trivial (API handlers run DB work in
worker threads via ``asyncio.to_thread``), and a broken/removed database file
is noticed on the next operation instead of being hidden by a cached handle.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from app.db import queries
from app.logs import get_logger, log_event

log = get_logger(__name__)

SCHEMA_VERSION = 1

_MIGRATION_NAME = re.compile(r"(\d{3})_[a-z0-9_]+\.sql")


class MigrationError(Exception):
    """The bundled migration files don't form the sequence 2..SCHEMA_VERSION."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Invalid migrations:\n" + "\n".join(f"  - {p}" for p in problems))


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self, *, create: bool = False) -> sqlite3.Connection:
        """Open a configured connection.

        Normal operation uses ``mode=rw`` so a deleted database file surfaces as
        an error (and /health goes 503) instead of silently creating an empty
        database. Only :meth:`initialize` may create the file.
        """
        mode = "rwc" if create else "rw"
        uri = f"{self.path.resolve().as_uri()}?mode={mode}"
        # isolation_level=None: we issue BEGIN/COMMIT explicitly.
        conn = sqlite3.connect(
            uri,
            uri=True,
            isolation_level=None,
            timeout=queries.PRAGMA_BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        try:
            queries.configure_connection(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Write transaction (``BEGIN IMMEDIATE``): commit on success, roll back on error.

        IMMEDIATE takes the write lock up front, so two writers never deadlock
        trying to upgrade read locks.
        """
        conn = self.connect()
        try:
            conn.execute(queries.BEGIN_IMMEDIATE)
            try:
                yield conn
            except BaseException:
                conn.execute(queries.ROLLBACK)
                raise
            conn.execute(queries.COMMIT)
        finally:
            conn.close()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        """Read-only unit of work in one consistent snapshot."""
        conn = self.connect()
        try:
            conn.execute(queries.BEGIN_DEFERRED)
            try:
                yield conn
            finally:
                conn.execute(queries.ROLLBACK)
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create the database file, enable WAL and apply pending migrations."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect(create=True)
        try:
            mode = queries.enable_wal(conn)
            if mode != "wal":
                log_event(log, logging.WARNING, "db.wal_unavailable", journal_mode=mode)
            version = queries.get_user_version(conn)
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {version} is newer than this build "
                    f"({SCHEMA_VERSION})"
                )
            for target, script in _pending_migrations(version):
                queries.apply_migration_script(conn, script, target)
                log_event(log, logging.INFO, "db.migrated", schema_version=target)
        finally:
            conn.close()


def _pending_migrations(current: int) -> list[tuple[int, str]]:
    """Version 1 is ``schema.sql``; later ones are ``migrations/NNN_name.sql``.

    All file names are validated before anything is returned, so a broken
    release fails at startup without touching the database.
    """
    package = resources.files("app.db")
    migrations = package.joinpath("migrations")
    names = [e.name for e in migrations.iterdir()] if migrations.is_dir() else []
    by_number = validate_migration_names(names, SCHEMA_VERSION)
    steps: list[tuple[int, str]] = []
    if current < 1:
        steps.append((1, package.joinpath("schema.sql").read_text(encoding="utf-8")))
    for number in sorted(n for n in by_number if n > max(current, 1)):
        steps.append((number, migrations.joinpath(by_number[number]).read_text(encoding="utf-8")))
    return steps


def validate_migration_names(names: list[str], schema_version: int) -> dict[int, str]:
    """Map migration number -> file name; the numbers must be exactly 2..schema_version.

    Non-``.sql`` files are ignored. Raises :class:`MigrationError` listing every problem.
    """
    problems: list[str] = []
    by_number: dict[int, str] = {}
    for name in sorted(n for n in names if n.endswith(".sql")):
        match = _MIGRATION_NAME.fullmatch(name)
        if match is None:
            problems.append(f"{name}: name must be NNN_description.sql (lowercase)")
            continue
        number = int(match.group(1))
        if number in by_number:
            problems.append(f"{name}: duplicate number {number} (also {by_number[number]})")
        elif number < 2:
            problems.append(f"{name}: numbers start at 2 (1 is schema.sql)")
        elif number > schema_version:
            problems.append(f"{name}: number {number} is above SCHEMA_VERSION {schema_version}")
        else:
            by_number[number] = name
    missing = sorted(set(range(2, schema_version + 1)) - by_number.keys())
    if missing:
        problems.append(f"missing migration(s) for version(s) {missing} "
                        f"(SCHEMA_VERSION is {schema_version})")
    if problems:
        raise MigrationError(problems)
    return by_number
