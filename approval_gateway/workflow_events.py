"""Persist authenticated ERP workflow triggers for asynchronous processing."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from .config import Settings
from .db import connection, utc_now
from .repository import authenticate_client
from .service import ServiceError


def init_workflow_events(settings: Settings) -> None:
    # Keep event acceptance durable and separate from ERP network calls.
    with connection(settings) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_system TEXT NOT NULL REFERENCES api_clients(system_name),
                event_id TEXT NOT NULL,
                request_type TEXT NOT NULL,
                reference_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (source_system, event_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS workflow_events_due
            ON workflow_events (status, next_attempt_at, id)
        """)


def _identifier(value: Any, name: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ServiceError(f"Invalid {name}.")
    return value


def accept_workflow_event(
    settings: Settings,
    api_key: str,
    data: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    # Accept only record identity; callers cannot choose recipients or endpoints.
    required = {"event_id", "source_system", "request_type", "reference_id"}
    if not isinstance(data, dict) or set(data) != required:
        raise ServiceError("Exactly four workflow event fields are required.")

    event_id = _identifier(data["event_id"], "event_id", 36)
    try:
        if str(UUID(event_id)) != event_id.lower():
            raise ValueError()
    except ValueError:
        raise ServiceError("event_id must be a hyphenated UUID.") from None

    normalized = {
        "event_id": event_id.lower(),
        "source_system": _identifier(
            data["source_system"], "source_system", 200
        ),
        "request_type": _identifier(data["request_type"], "request_type", 200),
        "reference_id": _identifier(data["reference_id"], "reference_id", 200),
    }
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))

    # Serialize competing retries and authenticate the event's source system.
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        client = authenticate_client(conn, api_key)
        if client is None:
            raise ServiceError("Invalid API key.", 401)
        if client["system_name"] != normalized["source_system"]:
            raise ServiceError("Source system mismatch.", 403)

        existing = conn.execute(
            "SELECT payload_json FROM workflow_events WHERE source_system=? AND event_id=?",
            (normalized["source_system"], normalized["event_id"]),
        ).fetchone()
        if existing is not None:
            if existing["payload_json"] != payload:
                raise ServiceError(
                    "Event ID already exists with different data.", 409
                )
            created = False
        else:
            now = utc_now()
            conn.execute(
                """
                INSERT INTO workflow_events (
                    source_system, event_id, request_type, reference_id,
                    payload_json, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    normalized["source_system"],
                    normalized["event_id"],
                    normalized["request_type"],
                    normalized["reference_id"],
                    payload,
                    now,
                    now,
                    now,
                ),
            )
            created = True

    # Acceptance confirms persistence, not WhatsApp delivery or ERP execution.
    return {"accepted": True, "event_id": normalized["event_id"]}, created
