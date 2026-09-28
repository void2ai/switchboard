-- Switchboard schema, version 1 (spec §6).
--
-- All timestamps are UTC ISO 8601 strings in one fixed-width format
-- (YYYY-MM-DDTHH:MM:SS.ffffffZ), so string comparison is time comparison.
-- This file is applied once, inside a transaction, when PRAGMA user_version = 0.

-- One row per outbound delivery or inbound Telegram update we chose to record.
-- Outbound statuses: pending -> sending -> sent | retry | dead
-- Inbound statuses:  received | processing -> handled | failed ; ignored
CREATE TABLE messages (
    id                  TEXT PRIMARY KEY,               -- UUID4, server generated
    direction           TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    source              TEXT NOT NULL,                  -- producer name, 'switchboard' or 'telegram'
    destination         TEXT NOT NULL,                  -- 'owner' (outbound) or 'switchboard' (inbound)
    kind                TEXT NOT NULL CHECK (kind IN ('event', 'reply', 'command', 'update')),
    topic               TEXT NOT NULL,
    severity            TEXT CHECK (severity IS NULL OR severity IN ('info', 'warning', 'error')),
    title               TEXT,
    body                TEXT,                           -- outbound text, or normalized command (never raw chat text)
    correlation_id      TEXT,                           -- producer's id, or inbound message id for replies
    status              TEXT NOT NULL CHECK (status IN (
                            'pending', 'sending', 'sent', 'retry', 'dead',
                            'received', 'processing', 'handled', 'failed', 'ignored')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TEXT,
    lease_until         TEXT,
    telegram_chat_id    INTEGER,
    telegram_message_id INTEGER,
    telegram_update_id  INTEGER,                        -- inbound only; guards against double processing
    created_at          TEXT NOT NULL,
    sent_at             TEXT,
    last_error_code     TEXT,
    -- Status must belong to the row's direction.
    CHECK (direction = 'outbound' OR status IN ('received', 'processing', 'handled', 'failed', 'ignored')),
    CHECK (direction = 'inbound' OR status IN ('pending', 'sending', 'sent', 'retry', 'dead'))
);

-- Worker lookup of due rows.
CREATE INDEX idx_messages_delivery ON messages (status, next_attempt_at);
-- Recent messages / audit.
CREATE INDEX idx_messages_created ON messages (created_at);
-- Startup recovery of unfinished inbound commands.
CREATE INDEX idx_messages_direction_status ON messages (direction, status);
-- A Telegram update can be recorded at most once.
CREATE UNIQUE INDEX idx_messages_update_id ON messages (telegram_update_id)
    WHERE telegram_update_id IS NOT NULL;

-- Producer idempotency keys, scoped per authenticated source.
CREATE TABLE idempotency_keys (
    source       TEXT NOT NULL,
    key          TEXT NOT NULL,
    request_hash TEXT NOT NULL,                         -- SHA-256 of canonical validated payload
    message_id   TEXT NOT NULL REFERENCES messages (id),
    created_at   TEXT NOT NULL,
    PRIMARY KEY (source, key)
);

-- Singleton: next Telegram update offset to request.
CREATE TABLE telegram_state (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    next_update_id INTEGER NOT NULL DEFAULT 0
);
INSERT INTO telegram_state (id, next_update_id) VALUES (1, 0);

-- Singleton written by GET /health to prove the database accepts writes.
CREATE TABLE health_probe (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    checked_at TEXT
);
INSERT INTO health_probe (id, checked_at) VALUES (1, NULL);
