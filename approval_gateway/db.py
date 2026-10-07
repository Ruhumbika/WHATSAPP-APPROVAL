from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import Settings

# Allow brief lock contention between the HTTP server and worker.
DATABASE_TIMEOUT_SECONDS = 10.0


def utc_now() -> str:
    # Store timestamps consistently in UTC, without microseconds.
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(settings: Settings) -> sqlite3.Connection:
    # Create the storage directory before opening the database.
    Path(settings.database_path).parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = sqlite3.connect(
        settings.database_path,
        timeout=DATABASE_TIMEOUT_SECONDS,
        isolation_level="DEFERRED",
    )

    try:
        conn.row_factory = sqlite3.Row

        # Foreign-key enforcement must be enabled on every connection.
        conn.execute("PRAGMA foreign_keys = ON")

        enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]

        if enabled != 1:
            raise RuntimeError(
                "SQLite foreign-key enforcement is unavailable."
            )

        return conn
    except BaseException:
        conn.close()
        raise


@contextmanager
def connection(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = connect(settings)

    try:
        yield conn
        conn.commit()
    except BaseException:
        # Roll back failed or interrupted work before closing.
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(settings: Settings) -> None:
    # Keep the original schema; subsequent changes belong in migrations.
    with connection(settings) as conn:
        conn.executescript("""
            BEGIN IMMEDIATE;

            -- Systems permitted to call the gateway.
            CREATE TABLE IF NOT EXISTS api_clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                system_name TEXT NOT NULL UNIQUE,
                api_key_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Registered recipients and their delivery phone numbers.
            CREATE TABLE IF NOT EXISTS approvers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                company TEXT NOT NULL DEFAULT 'all',
                phone_number TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Legacy routing retained for existing integrations.
            CREATE TABLE IF NOT EXISTS approval_routing_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_type TEXT NOT NULL,
                company TEXT NOT NULL,
                condition_json TEXT,
                approver_role TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Approval tasks received from source systems.
            CREATE TABLE IF NOT EXISTS approval_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gateway_reference_id TEXT NOT NULL UNIQUE,
                source_reference_id TEXT NOT NULL,
                source_system TEXT NOT NULL,
                request_type TEXT NOT NULL,
                company TEXT NOT NULL,
                message TEXT NOT NULL,
                amount REAL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                approver_id INTEGER NOT NULL,
                callback_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                whatsapp_message_id TEXT,
                responded_at TEXT,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (approver_id)
                    REFERENCES approvers(id)
            );

            -- Persist callback delivery state for worker retries.
            CREATE TABLE IF NOT EXISTS callback_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_request_id INTEGER NOT NULL,
                callback_url TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (approval_request_id)
                    REFERENCES approval_requests(id)
            );

            -- Provider event records; deduplication is handled separately.
            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                event_id TEXT,
                direction TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            -- Support request lookup, expiry scans and callback retries.
            CREATE INDEX IF NOT EXISTS idx_requests_source_reference
                ON approval_requests (
                    source_system,
                    source_reference_id
                );

            CREATE INDEX IF NOT EXISTS idx_requests_expiry
                ON approval_requests (status, expires_at);

            CREATE INDEX IF NOT EXISTS idx_callbacks_due
                ON callback_attempts (status, next_attempt_at);

            CREATE TABLE IF NOT EXISTS decision_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_request_id INTEGER NOT NULL REFERENCES approval_requests(id),
                kind TEXT NOT NULL CHECK (kind IN ('received', 'result')),
                phone TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','sending','sent','simulated','failed','unknown','expired')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                message_id TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (approval_request_id, kind)
            );
            CREATE INDEX IF NOT EXISTS decision_notifications_due
                ON decision_notifications(status, next_attempt_at);
            CREATE TABLE IF NOT EXISTS decision_repeat_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_request_id INTEGER NOT NULL REFERENCES approval_requests(id),
                inbound_message_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK (kind IN ('received', 'result')),
                phone TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','sending','sent','simulated','failed','unknown','expired')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                message_id TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            COMMIT;
            """)
