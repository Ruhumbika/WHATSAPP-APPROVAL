from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import Settings
from .db import utc_now
from .security import hash_api_key, normalize_phone, verify_api_key

# Repositories use the caller's transaction and never commit independently.


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")

    return value.strip()


def _serialize_json(value: dict[str, Any]) -> str:
    # Stable serialization prevents differences caused by dictionary key order.
    if not isinstance(value, dict):
        raise ValueError("JSON payload must be an object.")

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _non_negative_decimal(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Amount must be a valid number.") from None

    if not number.is_finite() or number < 0:
        raise ValueError("Amount must be finite and non-negative.")

    return number


def authenticate_client(
    conn: sqlite3.Connection,
    api_key: str,
) -> dict[str, Any] | None:
    # Authentication identifies the source system, not the approving user.
    if not isinstance(api_key, str) or not api_key.strip():
        return None

    for row in conn.execute("SELECT * FROM api_clients WHERE active = 1"):
        if verify_api_key(api_key, row["api_key_hash"]):
            return dict(row)

    return None


def create_api_client(
    conn: sqlite3.Connection,
    system_name: str,
    api_key: str,
) -> None:
    system_name = _required_text(system_name, "system_name")
    key_hash = hash_api_key(api_key)
    now = utc_now()

    # Updating an existing client rotates its key without reactivating it.
    conn.execute(
        """
        INSERT INTO api_clients (
            system_name,
            api_key_hash,
            active,
            created_at,
            updated_at
        )
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(system_name) DO UPDATE SET
            api_key_hash = excluded.api_key_hash,
            updated_at = excluded.updated_at
        """,
        (system_name, key_hash, now, now),
    )


def upsert_approver(
    conn: sqlite3.Connection,
    name: str,
    role: str,
    company: str,
    phone_number: str,
) -> None:
    name = _required_text(name, "name")
    role = _required_text(role, "role")
    company = _required_text(company, "company")
    phone_number = normalize_phone(phone_number)
    now = utc_now()

    existing = conn.execute(
        """
        SELECT id
        FROM approvers
        WHERE role = ?
          AND company = ?
          AND phone_number = ?
        """,
        (role, company, phone_number),
    ).fetchone()

    if existing:
        # Editing a recipient must not silently undo an administrative disable.
        conn.execute(
            """
            UPDATE approvers
            SET name = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, now, existing["id"]),
        )
        return

    conn.execute(
        """
        INSERT INTO approvers (
            name,
            role,
            company,
            phone_number,
            active,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, 1, ?, ?)
        """,
        (name, role, company, phone_number, now, now),
    )


def _validate_condition(condition: dict[str, Any]) -> None:
    # Reject unsupported rules instead of treating them as unconditional.
    allowed = {
        "amount_below",
        "amount_above",
        "amount_min",
        "amount_max",
        "payload_equals",
    }

    if not isinstance(condition, dict):
        raise ValueError("Routing condition must be an object.")

    if set(condition) - allowed:
        raise ValueError("Routing condition contains unsupported fields.")

    for field in allowed - {"payload_equals"}:
        if field in condition:
            _non_negative_decimal(condition[field])

    if "payload_equals" in condition and not isinstance(
        condition["payload_equals"], dict
    ):
        raise ValueError("payload_equals must be an object.")


def upsert_routing_rule(
    conn: sqlite3.Connection,
    request_type: str,
    company: str,
    condition: dict[str, Any] | None,
    approver_role: str,
    priority: int,
) -> None:
    request_type = _required_text(request_type, "request_type")
    company = _required_text(company, "company")
    approver_role = _required_text(approver_role, "approver_role")

    if type(priority) is not int or priority < 0:
        raise ValueError("priority must be a non-negative integer.")

    condition = {} if condition is None else condition
    _validate_condition(condition)
    condition_json = _serialize_json(condition)
    now = utc_now()

    existing = conn.execute(
        """
        SELECT id
        FROM approval_routing_rules
        WHERE request_type = ?
          AND company = ?
          AND condition_json = ?
          AND approver_role = ?
        ORDER BY id
        LIMIT 1
        """,
        (request_type, company, condition_json, approver_role),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE approval_routing_rules
            SET priority = ?, updated_at = ?
            WHERE id = ?
            """,
            (priority, now, existing["id"]),
        )
        return

    conn.execute(
        """
        INSERT INTO approval_routing_rules (
            request_type,
            company,
            condition_json,
            approver_role,
            priority,
            active,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            request_type,
            company,
            condition_json,
            approver_role,
            priority,
            now,
            now,
        ),
    )


def _condition_matches(
    condition: dict[str, Any],
    amount: float | None,
    payload: dict[str, Any],
) -> bool:
    _validate_condition(condition)

    # Decimal comparisons avoid introducing additional float rounding.
    amount_fields = {
        "amount_below",
        "amount_above",
        "amount_min",
        "amount_max",
    }

    if amount_fields.intersection(condition):
        if amount is None:
            return False

        actual = _non_negative_decimal(amount)

        for field in amount_fields.intersection(condition):
            expected = _non_negative_decimal(condition[field])

            if field == "amount_below" and actual >= expected:
                return False
            if field == "amount_above" and actual <= expected:
                return False
            if field == "amount_min" and actual < expected:
                return False
            if field == "amount_max" and actual > expected:
                return False

    for key, expected in condition.get("payload_equals", {}).items():
        if key not in payload or payload[key] != expected:
            return False

    return True


def resolve_approver(
    conn: sqlite3.Connection,
    request_type: str,
    company: str,
    amount: float | None,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    # Legacy routing only; ERP-selected actors use their registered bindings.
    rules = conn.execute(
        """
        SELECT *
        FROM approval_routing_rules
        WHERE active = 1
          AND request_type = ?
          AND company IN (?, 'all')
        ORDER BY priority ASC, id ASC
        """,
        (request_type, company),
    )

    for rule in rules:
        condition = json.loads(rule["condition_json"] or "{}")

        if not _condition_matches(condition, amount, payload):
            continue

        candidates = conn.execute(
            """
            SELECT *
            FROM approvers
            WHERE active = 1
              AND role = ?
              AND company IN (?, 'all')
            ORDER BY CASE WHEN company = ? THEN 0 ELSE 1 END, id
            """,
            (rule["approver_role"], company, company),
        ).fetchall()

        if not candidates:
            continue

        # Prefer the company scope, but never select an arbitrary person.
        selected_scope = candidates[0]["company"]
        matching = [
            row for row in candidates if row["company"] == selected_scope
        ]

        if len(matching) != 1:
            raise LookupError(
                "Routing matched multiple approvers; "
                "use an explicit actor binding."
            )

        return dict(matching[0]), dict(rule)

    raise LookupError(
        f"No active approver routing matched {company}:{request_type}"
    )


def create_approval_request(
    conn: sqlite3.Connection,
    settings: Settings,
    data: dict[str, Any],
    approver_id: int,
    source_system: str,
) -> dict[str, Any]:
    # The service validates scope and captures the actor in this transaction.
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    expires_at = created_at + timedelta(hours=settings.default_expiry_hours)

    gateway_reference_id = "AGR-" + secrets.token_hex(16).upper()
    payload = data.get("payload", {})

    cursor = conn.execute(
        """
        INSERT INTO approval_requests (
            gateway_reference_id,
            source_reference_id,
            source_system,
            request_type,
            company,
            message,
            amount,
            payload_json,
            approver_id,
            callback_url,
            status,
            expires_at,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
            gateway_reference_id,
            data["reference_id"],
            source_system,
            data["request_type"],
            data["company"],
            data["message"],
            data.get("amount"),
            _serialize_json(payload),
            approver_id,
            data["callback_url"],
            expires_at.isoformat(),
            created_at.isoformat(),
            created_at.isoformat(),
        ),
    )

    row = conn.execute(
        "SELECT * FROM approval_requests WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()

    if row is None:
        raise RuntimeError("Created approval request could not be read.")

    return dict(row)


def expire_due_requests(conn: sqlite3.Connection) -> int:
    # Expiry changes task state; it does not execute a source-system decision.
    now = utc_now()

    cursor = conn.execute(
        """
        UPDATE approval_requests
        SET status = 'expired', updated_at = ?
        WHERE status = 'pending'
          AND expires_at IS NOT NULL
          AND expires_at <= ?
        """,
        (now, now),
    )

    return cursor.rowcount
