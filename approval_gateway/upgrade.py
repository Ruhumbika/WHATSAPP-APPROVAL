"""Upgrade schema while preserving task and conversation records."""

import sqlite3

from .config import Settings
from .db import connection, utc_now

# Fixed migration definitions; identifiers never come from request input.
_REQUEST_COLUMNS = {
    "decision_rejection_json": "TEXT",
    "rejection_phase": "TEXT NOT NULL DEFAULT 'closed' CHECK(rejection_phase IN ('choice','reason','closed'))",
    "rejection_mode": "TEXT CHECK(rejection_mode IN ('correction','final'))",
    "rejection_expires_at": "TEXT",
    "rejection_updated_at": "TEXT",
    "step_id": "TEXT NOT NULL DEFAULT 'default'",
    "workflow_version": "TEXT NOT NULL DEFAULT '1'",
    "idempotency_key": "TEXT",
    "request_hash": "TEXT",
    "actor_user_id": "TEXT",
    "actor_name": "TEXT",
    "actor_phone": "TEXT",
    "decision_message_id": "TEXT",
    "original_reply": "TEXT",
    "execution_status": "TEXT NOT NULL DEFAULT 'not_requested'",
    "delivery_status": "TEXT NOT NULL DEFAULT 'pending'",
    "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
    "delivery_next_at": "TEXT",
    "delivery_error": "TEXT",
    "executed_at": "TEXT",
    "actor_directory_uuid": "TEXT",
    "actor_binding_id": "INTEGER",
}


# Keep existing table layouts compatible with current callers.
_SCHEMA_STATEMENTS = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS request_idempotency
        ON approval_requests (source_system, idempotency_key)
    """,
    """
    CREATE TABLE IF NOT EXISTS integrations (
        system_name TEXT PRIMARY KEY,
        callback_url TEXT NOT NULL,
        callback_secret TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS identities (
        approver_id INTEGER PRIMARY KEY
            REFERENCES approvers(id),
        source_user_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS actor_bindings (
        id INTEGER PRIMARY KEY,
        system_name TEXT NOT NULL
            REFERENCES api_clients(system_name),
        company TEXT NOT NULL,
        directory_uuid TEXT NOT NULL,
        source_user_id TEXT,
        approver_id INTEGER NOT NULL
            REFERENCES approvers(id),
        active INTEGER NOT NULL DEFAULT 1,
        UNIQUE (system_name, company, directory_uuid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY,
        request_id INTEGER REFERENCES approval_requests(id),
        event TEXT NOT NULL,
        details_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inbound_messages (
        message_id TEXT PRIMARY KEY,
        received_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_requests_delivery_due
        ON approval_requests (
            status,
            delivery_status,
            delivery_next_at
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_requests_whatsapp_message
        ON approval_requests (whatsapp_message_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_request
        ON audit_log (request_id, id)
    """,
)


def _add_request_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(approval_requests)")
    }

    if not columns:
        raise RuntimeError(
            "Base schema is missing. Run init_db() before migrate()."
        )

    # Existing columns and their stored values remain unchanged.
    for name, definition in _REQUEST_COLUMNS.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE approval_requests "
                f"ADD COLUMN {name} {definition}"
            )


def _backfill_delivery_state(conn: sqlite3.Connection) -> None:
    # Preserve evidence of prior delivery without requeueing those tasks.
    conn.execute("""
        UPDATE approval_requests
        SET delivery_status = 'sent'
        WHERE delivery_status = 'pending'
          AND delivery_attempts = 0
          AND delivery_next_at IS NULL
          AND whatsapp_message_id IS NOT NULL
          AND TRIM(whatsapp_message_id) <> ''
        """)

    # Schedule only pending, unsent tasks with an actor phone snapshot.
    conn.execute(
        """
        UPDATE approval_requests
        SET delivery_next_at = ?
        WHERE status = 'pending'
          AND delivery_status = 'pending'
          AND delivery_next_at IS NULL
          AND (
              whatsapp_message_id IS NULL
              OR TRIM(whatsapp_message_id) = ''
          )
          AND actor_phone IS NOT NULL
          AND TRIM(actor_phone) <> ''
        """,
        (utc_now(),),
    )


def _move_rejection_conversations(conn: sqlite3.Connection) -> None:
    """Upgrade the previous patch atomically, without losing active conversations."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rejection_conversations'"
    ).fetchone():
        return
    invalid = conn.execute("""
        SELECT 1 FROM rejection_conversations c
        LEFT JOIN approval_requests r ON r.id=c.approval_request_id
        WHERE r.id IS NULL OR r.actor_phone IS NULL OR r.actor_phone != c.phone
        LIMIT 1
    """).fetchone()
    if invalid:
        raise RuntimeError("Legacy rejection conversation has no matching task actor; migration rolled back.")
    conn.execute("""
        UPDATE approval_requests
        SET rejection_phase = (SELECT phase FROM rejection_conversations WHERE approval_request_id=approval_requests.id),
            rejection_mode = (SELECT mode FROM rejection_conversations WHERE approval_request_id=approval_requests.id),
            rejection_expires_at = (SELECT expires_at FROM rejection_conversations WHERE approval_request_id=approval_requests.id),
            rejection_updated_at = (SELECT updated_at FROM rejection_conversations WHERE approval_request_id=approval_requests.id)
        WHERE EXISTS (
            SELECT 1 FROM rejection_conversations c WHERE c.approval_request_id=approval_requests.id
              AND (approval_requests.rejection_updated_at IS NULL OR c.updated_at > approval_requests.rejection_updated_at)
        )
    """)
    conn.execute("DROP TABLE rejection_conversations")


def migrate(settings: Settings) -> None:
    with connection(settings) as conn:
        # Acquire the write lock before inspecting or changing the schema.
        conn.execute("BEGIN IMMEDIATE")

        _add_request_columns(conn)
        _move_rejection_conversations(conn)

        # Execute individually to preserve the surrounding transaction.
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(decision_repeat_notifications)")}
        if "payload_json" not in columns:
            conn.execute("ALTER TABLE decision_repeat_notifications ADD COLUMN payload_json TEXT")

        from .workflow_notifications import init_schema
        init_schema(conn)
        from .inbox import init_schema as init_inbox_schema
        init_inbox_schema(conn)
        _backfill_delivery_state(conn)
