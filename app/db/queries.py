"""All application SQL (spec §2).

Every function takes an open ``sqlite3.Connection`` and uses parameterized
statements only. Transactions are controlled by the caller (see
``app.db.connection.Database.transaction``); functions here never commit.
DDL lives in ``schema.sql``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

# --- Connection / transaction control ------------------------------------

PRAGMA_BUSY_TIMEOUT_MS = 5000

CONNECTION_PRAGMAS = (
    f"PRAGMA busy_timeout = {PRAGMA_BUSY_TIMEOUT_MS}",
    "PRAGMA foreign_keys = ON",
    # WAL + NORMAL is durable against application crashes; a power loss can
    # drop the last committed transactions but never corrupts the database.
    "PRAGMA synchronous = NORMAL",
)
BEGIN_IMMEDIATE = "BEGIN IMMEDIATE"
BEGIN_DEFERRED = "BEGIN"
COMMIT = "COMMIT"
ROLLBACK = "ROLLBACK"


def configure_connection(conn: sqlite3.Connection) -> None:
    for pragma in CONNECTION_PRAGMAS:
        conn.execute(pragma)


def enable_wal(conn: sqlite3.Connection) -> str:
    """Switch to WAL (persistent per database file); returns the active mode."""
    return str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()


def get_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_migration_script(conn: sqlite3.Connection, script: str, version: int) -> None:
    """Run a DDL script and bump user_version atomically.

    ``executescript`` manages transactions itself, so BEGIN/COMMIT are part of
    the script: either the whole migration applies or none of it does.
    """
    conn.executescript(
        f"{BEGIN_IMMEDIATE};\n{script}\nPRAGMA user_version = {int(version)};\n{COMMIT};"
    )


def integrity_check(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


# --- Messages -------------------------------------------------------------

_MESSAGE_COLUMNS = (
    "id", "direction", "source", "destination", "kind", "topic", "severity", "title", "body",
    "correlation_id", "status", "attempts", "next_attempt_at", "lease_until",
    "telegram_chat_id", "telegram_message_id", "telegram_update_id", "created_at", "sent_at",
    "last_error_code",
)


def insert_message(conn: sqlite3.Connection, **values: Any) -> None:
    """Insert a messages row. Unknown column names raise ``ValueError``."""
    unknown = set(values) - set(_MESSAGE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown message columns: {sorted(unknown)}")
    columns = [c for c in _MESSAGE_COLUMNS if c in values]
    # Column names come from the fixed whitelist above, never from input.
    sql = (
        f"INSERT INTO messages ({', '.join(columns)}) "  # noqa: S608
        f"VALUES ({', '.join(':' + c for c in columns)})"
    )
    conn.execute(sql, values)


def get_message(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()


def count_messages(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])


# --- Idempotency ----------------------------------------------------------

def get_idempotency_record(
    conn: sqlite3.Connection, source: str, key: str
) -> sqlite3.Row | None:
    """Return (request_hash, message_id, status) for an existing key."""
    return conn.execute(
        """
        SELECT k.request_hash, k.message_id, m.status
        FROM idempotency_keys AS k
        JOIN messages AS m ON m.id = k.message_id
        WHERE k.source = ? AND k.key = ?
        """,
        (source, key),
    ).fetchone()


def insert_idempotency_record(
    conn: sqlite3.Connection, source: str, key: str, request_hash: str, message_id: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO idempotency_keys (source, key, request_hash, message_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, key, request_hash, message_id, created_at),
    )


# --- Outbound delivery queue ---------------------------------------------

def select_next_due(conn: sqlite3.Connection, now: str) -> sqlite3.Row | None:
    """Oldest outbound row that is due, or whose ``sending`` lease expired."""
    return conn.execute(
        """
        SELECT * FROM messages
        WHERE direction = 'outbound'
          AND (
                (status IN ('pending', 'retry') AND next_attempt_at <= :now)
             OR (status = 'sending' AND lease_until <= :now)
          )
        ORDER BY next_attempt_at, created_at
        LIMIT 1
        """,
        {"now": now},
    ).fetchone()


def mark_sending(conn: sqlite3.Connection, message_id: str, lease_until: str) -> None:
    """Claim a row: take a lease and count the attempt."""
    conn.execute(
        "UPDATE messages SET status = 'sending', lease_until = ?, attempts = attempts + 1 "
        "WHERE id = ?",
        (lease_until, message_id),
    )


def mark_sent(
    conn: sqlite3.Connection, message_id: str, chat_id: int, telegram_message_id: int | None,
    sent_at: str,
) -> None:
    conn.execute(
        """
        UPDATE messages
        SET status = 'sent', telegram_chat_id = ?, telegram_message_id = ?, sent_at = ?,
            lease_until = NULL, next_attempt_at = NULL, last_error_code = NULL
        WHERE id = ?
        """,
        (chat_id, telegram_message_id, sent_at, message_id),
    )


def mark_retry(
    conn: sqlite3.Connection, message_id: str, next_attempt_at: str, error_code: str,
    *, refund_attempt: bool = False,
) -> None:
    """Schedule another attempt. ``refund_attempt`` undoes the claim's increment
    (used when the failure was global, e.g. a bad bot token, not this message)."""
    conn.execute(
        """
        UPDATE messages
        SET status = 'retry', next_attempt_at = ?, lease_until = NULL, last_error_code = ?,
            attempts = CASE WHEN ? THEN MAX(attempts - 1, 0) ELSE attempts END
        WHERE id = ?
        """,
        (next_attempt_at, error_code, 1 if refund_attempt else 0, message_id),
    )


def mark_dead(conn: sqlite3.Connection, message_id: str, error_code: str) -> None:
    conn.execute(
        "UPDATE messages SET status = 'dead', lease_until = NULL, next_attempt_at = NULL, "
        "last_error_code = ? WHERE id = ?",
        (error_code, message_id),
    )


def next_wakeup_time(conn: sqlite3.Connection) -> str | None:
    """Earliest time at which some outbound row becomes due (or its lease expires)."""
    row = conn.execute(
        """
        SELECT MIN(t) FROM (
            SELECT MIN(next_attempt_at) AS t FROM messages
            WHERE direction = 'outbound' AND status IN ('pending', 'retry')
            UNION ALL
            SELECT MIN(lease_until) AS t FROM messages
            WHERE direction = 'outbound' AND status = 'sending'
        )
        """
    ).fetchone()
    return row[0] if row else None


def outbound_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM messages WHERE direction = 'outbound' GROUP BY status"
    ).fetchall()
    counts = {status: 0 for status in ("pending", "sending", "sent", "retry", "dead")}
    for status, count in rows:
        counts[status] = int(count)
    return counts


# --- Telegram inbound -----------------------------------------------------

def get_next_update_id(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT next_update_id FROM telegram_state WHERE id = 1").fetchone()[0]
    )


def advance_next_update_id(conn: sqlite3.Connection, next_update_id: int) -> None:
    """Move the offset forward (never backward)."""
    conn.execute(
        "UPDATE telegram_state SET next_update_id = MAX(next_update_id, ?) WHERE id = 1",
        (next_update_id,),
    )


def transition_inbound(
    conn: sqlite3.Connection, message_id: str, from_status: str, to_status: str,
    error_code: str | None = None,
) -> bool:
    """Compare-and-set an inbound row's status; returns True if it changed."""
    cursor = conn.execute(
        "UPDATE messages SET status = ?, last_error_code = COALESCE(?, last_error_code) "
        "WHERE id = ? AND direction = 'inbound' AND status = ?",
        (to_status, error_code, message_id, from_status),
    )
    return cursor.rowcount == 1


def list_processing_inbound(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE direction = 'inbound' AND status = 'processing' "
        "ORDER BY created_at"
    ).fetchall()


# --- Health ---------------------------------------------------------------

def touch_health_probe(conn: sqlite3.Connection, now: str) -> None:
    cursor = conn.execute("UPDATE health_probe SET checked_at = ? WHERE id = 1", (now,))
    if cursor.rowcount != 1:
        raise sqlite3.DatabaseError("health_probe row missing")
