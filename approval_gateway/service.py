from __future__ import annotations
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from .notifications import enqueue_notification, enqueue_repeat_notification
from .config import Settings
from .db import connection, utc_now
from .repository import authenticate_client, create_approval_request
from .security import normalize_phone
from .integrations.permissions import (
    IntegrationUnavailable,
    WorkflowActor,
    WorkflowDenied,
)
from .integrations.registry import get_workflow_guard


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _text(value: Any, field: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(f"{field} must be a non-empty string.")
    value = value.strip()
    if len(value) > maximum:
        raise ServiceError(f"{field} exceeds {maximum} characters.")
    return value


def _json(value: Any) -> str:

    # Use consistent JSON and reject unsupported or non-finite values.
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise ServiceError("Payload contains invalid JSON values.") from None


def _phone(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"\+?[1-9][0-9]{6,14}", value
    ):
        raise ServiceError("A valid international phone is required.")
    try:
        return normalize_phone(value)
    except ValueError:
        raise ServiceError(
            "A valid international phone is required."
        ) from None


def _expired(expires_at: Any) -> bool:

    # Missing or malformed expiry must not authorize a decision.
    if not isinstance(expires_at, str):
        raise ServiceError("Task expiry is missing.", 422)
    try:
        expiry = datetime.fromisoformat(expires_at)
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError()
        return expiry <= datetime.now(timezone.utc)
    except ValueError:
        raise ServiceError("Task expiry is invalid.", 422) from None


def audit(
    conn: sqlite3.Connection,
    request_id: int,
    event: str,
    details: dict[str, Any],
) -> None:

    # Audit changes belong to the same transaction as the recorded action.
    conn.execute(
        """

        INSERT INTO audit_log (

            request_id, event, details_json, created_at

        )

        VALUES (?, ?, ?, ?)

        """,
        (request_id, event, _json(details), utc_now()),
    )


def get_client(
    conn: sqlite3.Connection,
    key: str,
) -> dict[str, Any]:
    client = authenticate_client(conn, key)
    if client is None:
        raise ServiceError("Invalid API key.", 401)
    return client


def _validate_request(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ServiceError("JSON object required.")
    allowed = {
        "source_system",
        "company",
        "request_type",
        "reference_id",
        "message",
        "step_id",
        "workflow_version",
        "actor_directory_uuid",
        "payload",
        "amount",
        "callback_url",
        "idempotency_key",
    }
    if set(data) - allowed:
        raise ServiceError("Request contains unsupported fields.")
    result = dict(data)
    for field in (
        "company",
        "request_type",
        "reference_id",
        "step_id",
        "workflow_version",
    ):
        result[field] = _text(data.get(field), field)
    result["message"] = _text(data.get("message"), "message", 4096)

    # The ERP must explicitly select the actor for this workflow step.
    actor_uuid = _text(
        data.get("actor_directory_uuid"),
        "actor_directory_uuid",
        36,
    )
    try:
        result["actor_directory_uuid"] = str(UUID(actor_uuid))
    except ValueError:
        raise ServiceError("actor_directory_uuid is invalid.") from None
    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise ServiceError("payload must be an object.")

    # Detach the snapshot from mutable caller-owned objects.
    result["payload"] = json.loads(_json(payload))
    if data.get("amount") is not None:
        try:
            amount = Decimal(str(data["amount"]))
            if not amount.is_finite() or amount < 0:
                raise ValueError()
            stored_amount = float(amount)
            if not math.isfinite(stored_amount):
                raise ValueError()
            result["amount"] = stored_amount
        except (InvalidOperation, ValueError, OverflowError):
            raise ServiceError("Invalid amount.") from None
    else:
        result["amount"] = None
    if "idempotency_key" in data:
        result["idempotency_key"] = _text(
            data["idempotency_key"],
            "idempotency_key",
        )
    return result


def _assigned_actor(
    conn: sqlite3.Connection,
    system: str,
    company: str,
    directory_uuid: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    binding = conn.execute(
        """

        SELECT *

        FROM actor_bindings

        WHERE system_name = ?

          AND company = ?

          AND directory_uuid = ?

          AND active = 1

        """,
        (system, company, directory_uuid),
    ).fetchone()
    if binding is None:
        raise ServiceError(
            "Active actor binding missing for this system/company.",
            403,
        )
    approver = conn.execute(
        "SELECT * FROM approvers WHERE id = ? AND active = 1",
        (binding["approver_id"],),
    ).fetchone()
    if approver is None:
        raise ServiceError("Approver disabled.", 403)
    return dict(binding), dict(approver)


def _authorize_workflow(system: str, data: dict[str, Any]) -> WorkflowActor:

    # The registry selects the ERP adapter; the core never names an ERP.
    try:
        actor = get_workflow_guard(system).authorize(data)
    except WorkflowDenied as exc:
        raise ServiceError(str(exc), 403) from None
    except IntegrationUnavailable:
        raise ServiceError(
            "Workflow verification is unavailable or not configured.", 503
        ) from None
    if (
        actor.directory_uuid != data["actor_directory_uuid"]
        or actor.step_id != data["step_id"]
    ):
        raise ServiceError("Workflow actor or step mismatch.", 403)
    return actor


def _workflow_evidence(actor: WorkflowActor) -> dict[str, str]:
    return {
        "module_id": actor.module_id,
        "step_id": actor.step_id,
        "record_digest": actor.record_digest,
    }


def _validate_workflow_binding(actor, binding, phone) -> None:
    if actor is not None and (
        binding["source_user_id"] != actor.source_user_id
        or phone != _phone(actor.phone_number)
    ):
        raise ServiceError(
            "Gateway binding does not match the verified ERP actor.", 403
        )


def _preflight_decision(
    settings: Settings, response: dict[str, Any], phone: str
):

    # Validate the local actor first; perform external calls after closing the connection.
    with connection(settings) as conn:
        if conn.execute(
            "SELECT 1 FROM inbound_messages WHERE message_id=?",
            (response["message_id"],),
        ).fetchone():
            return None
        if response.get("reference"):
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE gateway_reference_id=?",
                (response["reference"],),
            ).fetchall()
        elif response.get("context_id"):
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE whatsapp_message_id=? LIMIT 2",
                (response["context_id"],),
            ).fetchall()
        else:
            return None
        if len(rows) != 1:
            return None
        task = dict(rows[0])
        if task["status"] != "pending" or _expired(task["expires_at"]):
            return None
        _check_actor(conn, task, phone)
        if (
            response.get("context_id")
            and response["context_id"] != task["whatsapp_message_id"]
        ):
            raise ServiceError("Message context does not match task.", 403)
    data = {
        "reference_id": task["source_reference_id"],
        "company": task["company"],
        "request_type": task["request_type"],
        "step_id": task["step_id"],
        "actor_directory_uuid": task["actor_directory_uuid"],
        "payload": json.loads(task["payload_json"]),
    }
    actor = _authorize_workflow(task["source_system"], data)
    if actor is not None:
        if (
            actor.source_user_id != task["actor_user_id"]
            or _phone(actor.phone_number) != phone
        ):
            raise ServiceError("ERP actor identity or contact changed.", 403)
        if data["payload"].get("_gateway_workflow") != _workflow_evidence(
            actor
        ):
            raise ServiceError(
                "ERP record changed; refresh the approval task.", 409
            )
    return task["id"], task["request_hash"]


def _prepare_task(
    system: str, data: dict[str, Any]
) -> tuple[dict[str, Any], WorkflowActor]:

    # Resolve current ERP authority before opening a database write transaction.
    data = _validate_request(data)
    if data.get("source_system", system) != system:
        raise ServiceError("Source system mismatch.", 403)
    if "_gateway_workflow" in data["payload"]:
        raise ServiceError("payload._gateway_workflow is reserved.")
    actor = _authorize_workflow(system, data)
    data["payload"]["_gateway_workflow"] = _workflow_evidence(actor)
    return data, actor


def _provision_verified_actor(
    conn: sqlite3.Connection,
    system: str,
    company: str,
    actor: WorkflowActor,
) -> tuple[dict[str, Any], dict[str, Any]]:
    existing = conn.execute(
        """
        SELECT *
        FROM actor_bindings
        WHERE system_name = ? AND company = ? AND directory_uuid = ?
        """,
        (system, company, actor.directory_uuid),
    ).fetchone()

    phone = _phone(actor.phone_number)

    if existing is not None:
        binding, approver = _assigned_actor(
            conn, system, company, actor.directory_uuid
        )
        if approver["company"] not in {company, "all"}:
            raise ServiceError("Approver company scope mismatch.", 403)
        _validate_workflow_binding(
            actor, binding, _phone(approver["phone_number"])
        )
        return binding, approver

    now = utc_now()

    # Create an identity-specific record; a shared phone is not identity proof.
    cursor = conn.execute(
        """
        INSERT INTO approvers (
            name, role, company, phone_number, active,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 1, ?, ?)
        """,
        (
            f"{system}:{actor.source_user_id}",
            "workflow_actor",
            company,
            phone,
            now,
            now,
        ),
    )
    approver_id = cursor.lastrowid

    conn.execute(
        """
        INSERT INTO actor_bindings (
            system_name, company, directory_uuid,
            source_user_id, approver_id, active
        )
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        (
            system,
            company,
            actor.directory_uuid,
            actor.source_user_id,
            approver_id,
        ),
    )

    return _assigned_actor(conn, system, company, actor.directory_uuid)


def _persist_task(
    conn: sqlite3.Connection,
    settings: Settings,
    system: str,
    data: dict[str, Any],
    verified_actor: WorkflowActor,
) -> tuple[dict[str, Any], bool]:

    # The caller owns the transaction so an event's candidate tasks commit together.
    integration = conn.execute(
        "SELECT * FROM integrations WHERE system_name = ?",
        (system,),
    ).fetchone()
    if integration is None:
        raise ServiceError("Register integration callback first.", 422)
    callback_url = integration["callback_url"]
    if data.get("callback_url", callback_url) != callback_url:
        raise ServiceError("Callback URL is not registered.", 403)

    # Transport options do not form part of the business snapshot.
    canonical = {
        key: value for key, value in data.items() if key != "idempotency_key"
    }
    canonical.update(
        source_system=system,
        callback_url=callback_url,
    )
    natural = [
        data["company"],
        data["actor_directory_uuid"],
        data["request_type"],
        data["reference_id"],
        data["step_id"],
        data["workflow_version"],
    ]
    idem = (
        data.get("idempotency_key")
        or hashlib.sha256(_json(natural).encode("utf-8")).hexdigest()
    )
    fingerprint = hashlib.sha256(_json(canonical).encode("utf-8")).hexdigest()
    existing = conn.execute(
        """

        SELECT *

        FROM approval_requests

        WHERE source_system = ?

          AND idempotency_key = ?

        """,
        (system, idem),
    ).fetchone()

    # A changed transport key must not create another copy of the same task.
    same_task = conn.execute(
        """

        SELECT *

        FROM approval_requests

        WHERE source_system = ?

          AND company = ?

          AND actor_directory_uuid = ?

          AND request_type = ?

          AND source_reference_id = ?

          AND step_id = ?

          AND workflow_version = ?

        ORDER BY id

        LIMIT 1

        """,
        (system, *natural),
    ).fetchone()
    if existing is not None or same_task is not None:
        stored = existing if existing is not None else same_task
        if stored["request_hash"] != fingerprint:
            raise ServiceError(
                "Existing task has different content; "
                "use a new workflow version.",
                409,
            )
        return dict(stored), False
    binding, approver = _provision_verified_actor(
        conn,
        system,
        data["company"],
        verified_actor,
    )

    actor_phone = _phone(approver["phone_number"])
    _validate_workflow_binding(verified_actor, binding, actor_phone)
    request = create_approval_request(
        conn,
        settings,
        canonical,
        approver["id"],
        system,
    )

    # Store the identity used at creation, independently of later edits.
    conn.execute(
        """

        UPDATE approval_requests

        SET step_id = ?,

            workflow_version = ?,

            idempotency_key = ?,

            request_hash = ?,

            actor_user_id = ?,

            actor_name = ?,

            actor_phone = ?,

            actor_directory_uuid = ?,

            actor_binding_id = ?,

            delivery_next_at = ?

        WHERE id = ?

        """,
        (
            data["step_id"],
            data["workflow_version"],
            idem,
            fingerprint,
            binding["source_user_id"],
            approver["name"],
            actor_phone,
            binding["directory_uuid"],
            binding["id"],
            utc_now(),
            request["id"],
        ),
    )
    audit(
        conn,
        request["id"],
        "created",
        {
            "step_id": data["step_id"],
            "workflow_version": data["workflow_version"],
            "actor_binding_id": binding["id"],
        },
    )
    return (
        dict(
            conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?",
                (request["id"],),
            ).fetchone()
        ),
        True,
    )


def create(
    settings: Settings,
    key: str,
    data: dict[str, Any],
) -> tuple[dict[str, Any], bool]:

    # Public task creation always authenticates with an API key.
    with connection(settings) as conn:
        system = get_client(conn, key)["system_name"]
    prepared, actor = _prepare_task(system, data)
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if get_client(conn, key)["system_name"] != system:
            raise ServiceError("API client changed during verification.", 409)
        return _persist_task(conn, settings, system, prepared, actor)


def process_workflow_event(
    settings: Settings, event_row_id: int
) -> dict[str, int]:

    # Only the worker calls this entry point after claiming a persisted event.
    if type(event_row_id) is not int or event_row_id <= 0:
        raise ServiceError("Invalid workflow event row ID.")
    with connection(settings) as conn:
        event = conn.execute(
            "SELECT * FROM workflow_events WHERE id = ? AND status = 'processing'",
            (event_row_id,),
        ).fetchone()
        if event is None:
            raise ServiceError("Workflow event is not claimed.", 409)
        event = dict(event)
        system = event["source_system"]
        if not conn.execute(
            "SELECT 1 FROM api_clients WHERE system_name = ? AND active = 1",
            (system,),
        ).fetchone():
            raise ServiceError("Source system disabled.", 403)
    envelope = json.loads(event["payload_json"])
    expected = {
        key: event[key]
        for key in (
            "event_id",
            "source_system",
            "request_type",
            "reference_id",
        )
    }
    if envelope != expected:
        raise ServiceError("Stored workflow event identity mismatch.", 409)
    try:
        adapter = get_workflow_guard(system)
        resolver = getattr(adapter, "resolve_workflow_event", None)
        if not callable(resolver):
            raise IntegrationUnavailable(
                "Workflow event resolver is not configured"
            )
        candidates = resolver(envelope)
        if not isinstance(candidates, (tuple, list)) or len(candidates) > 100:
            raise IntegrationUnavailable(
                "Invalid workflow candidate collection"
            )
    except WorkflowDenied as exc:
        raise ServiceError(str(exc), 403) from None
    except IntegrationUnavailable:
        raise ServiceError(
            "Workflow event resolution is unavailable.", 503
        ) from None
    prepared = []
    seen = set()
    for candidate in candidates:
        data, actor = _prepare_task(system, candidate)
        if (
            data["request_type"] != event["request_type"]
            or data["reference_id"] != event["reference_id"]
        ):
            raise ServiceError(
                "Candidate belongs to a different workflow event.", 403
            )
        if data["workflow_version"] != actor.record_digest:
            raise ServiceError(
                "ERP record changed during event resolution.", 409
            )
        scope = (
            data["company"],
            data["actor_directory_uuid"],
            data["step_id"],
            data["workflow_version"],
        )
        if scope in seen:
            raise ServiceError(
                "Adapter returned duplicate workflow candidates.", 409
            )
        seen.add(scope)
        prepared.append((data, actor))
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM workflow_events WHERE id = ?",
            (event_row_id,),
        ).fetchone()
        if (
            current is None
            or current["status"] != "processing"
            or current["attempts"] != event["attempts"]
            or current["payload_json"] != event["payload_json"]
        ):
            raise ServiceError("Workflow event claim changed.", 409)
        if not conn.execute(
            "SELECT 1 FROM api_clients WHERE system_name = ? AND active = 1",
            (system,),
        ).fetchone():
            raise ServiceError("Source system disabled.", 403)
            # Persist all current assignments before reconciling previous tasks.
        created = 0
        for data, actor in prepared:
            _, is_new = _persist_task(conn, settings, system, data, actor)
            created += int(is_new)

        current_scopes = {
            (
                data["company"],
                data["actor_directory_uuid"],
                data["step_id"],
                data["workflow_version"],
            )
            for data, _ in prepared
        }

        pending_tasks = conn.execute(
            """
            SELECT id, company, actor_directory_uuid,
              step_id, workflow_version
            FROM approval_requests
            WHERE source_system = ?
              AND request_type = ?
              AND source_reference_id = ?
              AND status = 'pending'
            """,
            (
                system,
                event["request_type"],
                event["reference_id"],
            ),
        ).fetchall()

        # Cancel assignments absent from the verified current ERP workflow.
        now = utc_now()
        for task in pending_tasks:
            scope = (
                task["company"],
                task["actor_directory_uuid"],
                task["step_id"],
                task["workflow_version"],
            )
            if scope in current_scopes:
                continue

            conn.execute(
                """
                UPDATE approval_requests
                SET status = 'cancelled', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, task["id"]),
            )
            audit(
                conn,
                task["id"],
                "cancelled",
                {
                    "reason": "ERP workflow assignment is no longer current.",
                    "workflow_event_id": event["event_id"],
                },
            )
        conn.execute(
            """UPDATE workflow_events
               SET status = 'completed', last_error = NULL, updated_at = ?
               WHERE id = ? AND status = 'processing'""",
            (utc_now(), event_row_id),
        )
    return {"tasks": len(prepared), "created": created}


def verify_delivery_workflow(settings: Settings, task: dict[str, Any]) -> None:

    # Recheck live assignment and content immediately before a queued message is sent.
    data = {
        "reference_id": task["source_reference_id"],
        "company": task["company"],
        "request_type": task["request_type"],
        "step_id": task["step_id"],
        "actor_directory_uuid": task["actor_directory_uuid"],
    }
    actor = _authorize_workflow(task["source_system"], data)
    if (
        actor.source_user_id != task["actor_user_id"]
        or _phone(actor.phone_number) != task["actor_phone"]
        or json.loads(task["payload_json"]).get("_gateway_workflow")
        != _workflow_evidence(actor)
    ):
        raise ServiceError(
            "Queued task no longer matches the ERP workflow.", 409
        )


def _check_actor(
    conn: sqlite3.Connection,
    request: dict[str, Any],
    phone: str,
) -> None:

    # Legacy tasks without a binding require explicit migration or reissue.
    if not request["actor_binding_id"]:
        raise ServiceError("Task has no actor binding.", 422)
    binding, approver = _assigned_actor(
        conn,
        request["source_system"],
        request["company"],
        request["actor_directory_uuid"],
    )
    if (
        binding["id"] != request["actor_binding_id"]
        or binding["source_user_id"] != request["actor_user_id"]
        or binding["approver_id"] != request["approver_id"]
    ):
        raise ServiceError("Actor binding revoked or changed.", 403)
    if (
        phone != request["actor_phone"]
        or _phone(approver["phone_number"]) != request["actor_phone"]
    ):
        raise ServiceError("Assigned actor phone mismatch.", 403)
    client = conn.execute(
        """

        SELECT 1

        FROM api_clients

        WHERE system_name = ? AND active = 1

        """,
        (request["source_system"],),
    ).fetchone()
    if client is None:
        raise ServiceError("Source system disabled.", 403)


def _callback_payload(
    request: dict[str, Any],
    response: dict[str, Any],
    now: str,
) -> dict[str, Any]:

    # A recorded decision remains separate from source-system execution.
    return {
        "event_id": "decision:" + request["gateway_reference_id"],
        "gateway_reference_id": request["gateway_reference_id"],
        "reference_id": request["source_reference_id"],
        "source_system": request["source_system"],
        "company": request["company"],
        "request_type": request["request_type"],
        "step_id": request["step_id"],
        "workflow_version": request["workflow_version"],
        "status": response["action"],
        "responded_at": now,
        "channel": "whatsapp",
        "actor": {
            "directory_uuid": request["actor_directory_uuid"],
            "user_id": request["actor_user_id"],
            "name": request["actor_name"],
            "phone": request["actor_phone"],
        },
        "evidence": {
            "message_id": response["message_id"],
            "original_reply": response["text"],
            "context_id": response.get("context_id"),
        },
        "payload": json.loads(request["payload_json"]),
    }


def decide(
    settings: Settings,
    response: dict[str, Any],
) -> dict[str, Any]:

    # Only the signature-verified webhook handler may call this entry point.
    if not isinstance(response, dict):
        raise ServiceError("Response must be an object.")
    response = dict(response)
    response["message_id"] = _text(
        response.get("message_id"), "message_id", 512
    )
    phone = _phone(response.get("phone"))
    if response.get("action") not in {"approved", "rejected"}:
        raise ServiceError("Invalid decision.")
    if not isinstance(response.get("text"), str):
        raise ServiceError("Original reply must be a string.")
    if len(response["text"]) > 4096:
        raise ServiceError("Original reply is too long.")
    for field in ("reference", "context_id"):
        if response.get(field) is not None:
            response[field] = _text(response[field], field, 512)
    preflight = _preflight_decision(settings, response, phone)
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            "SELECT 1 FROM inbound_messages WHERE message_id = ?",
            (response["message_id"],),
        ).fetchone()
        if duplicate is not None:
            return {"processed": False, "reason": "duplicate_message"}
        if response.get("reference"):
            row = conn.execute(
                """

                SELECT *

                FROM approval_requests

                WHERE gateway_reference_id = ?

                """,
                (response["reference"],),
            ).fetchone()
        elif response.get("context_id"):
            matches = conn.execute(
                """

                SELECT *

                FROM approval_requests

                WHERE whatsapp_message_id = ?

                LIMIT 2

                """,
                (response["context_id"],),
            ).fetchall()
            if len(matches) > 1:
                raise ServiceError("Ambiguous message context.", 409)
            row = matches[0] if matches else None
        else:
            return {"processed": False, "reason": "missing_task_reference"}
        if row is None:
            raise ServiceError("Approval task not found.", 404)
        request = dict(row)
        if phone != request["actor_phone"]:
            raise ServiceError("Responder is not the assigned actor.", 403)
        if (
            response.get("context_id")
            and response["context_id"] != request["whatsapp_message_id"]
        ):
            raise ServiceError("Message context does not match task.", 403)
        now = utc_now()
        if request["status"] == "pending":
            if _expired(request["expires_at"]):
                conn.execute(
                    """

                    UPDATE approval_requests

                    SET status = 'expired', updated_at = ?

                    WHERE id = ? AND status = 'pending'

                    """,
                    (now, request["id"]),
                )
                audit(conn, request["id"], "expired", {})
                request["status"] = "expired"
            else:
                _check_actor(conn, request, phone)
                if preflight != (request["id"], request["request_hash"]):
                    raise ServiceError(
                        "Task changed during workflow verification.", 409
                    )
        conn.execute(
            """

            INSERT INTO inbound_messages (message_id, received_at)

            VALUES (?, ?)

            """,
            (response["message_id"], now),
        )
        if request["status"] != "pending":
            enqueue_repeat_notification(conn, request, response, now)
            return {"processed": False, "reason": request["status"]}
        callback = _callback_payload(request, response, now)

        # Persist the decision, callback queue and audit atomically.
        cursor = conn.execute(
            """

            UPDATE approval_requests

            SET status = ?,

                responded_at = ?,

                updated_at = ?,

                decision_message_id = ?,

                original_reply = ?,

                execution_status = 'pending'

            WHERE id = ? AND status = 'pending'

            """,
            (
                response["action"],
                now,
                now,
                response["message_id"],
                response["text"],
                request["id"],
            ),
        )
        if cursor.rowcount != 1:
            raise ServiceError("Task state changed.", 409)
        conn.execute(
            """

            INSERT INTO callback_attempts (

                approval_request_id,

                callback_url,

                payload_json,

                status,

                attempts,

                next_attempt_at,

                created_at,

                updated_at

            )

            VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)

            """,
            (
                request["id"],
                request["callback_url"],
                _json(callback),
                now,
                now,
                now,
            ),
        )
        audit(
            conn,
            request["id"],
            "decision_received",
            {
                "event_id": callback["event_id"],
                "decision": response["action"],
                "actor_directory_uuid": request["actor_directory_uuid"],
                "message_id": response["message_id"],
            },
        )
        enqueue_notification(
            conn, request, "received", now, decision=response["action"]
        )
        return {
            "processed": True,
            "decision": response["action"],
            "execution_status": "pending",
        }


def _owned_request(
    conn: sqlite3.Connection,
    system: str,
    reference: str,
) -> dict[str, Any]:

    # API credentials authorize access at source-system scope.
    row = conn.execute(
        """

        SELECT *

        FROM approval_requests

        WHERE gateway_reference_id = ?

          AND source_system = ?

        """,
        (_text(reference, "reference"), system),
    ).fetchone()
    if row is None:
        raise ServiceError("Request not found.", 404)
    return dict(row)


def read_request(
    settings: Settings,
    key: str,
    reference: str,
) -> dict[str, Any]:
    with connection(settings) as conn:

        # Keep the task and its audit records in one read snapshot.
        conn.execute("BEGIN")
        client = get_client(conn, key)
        request = _owned_request(conn, client["system_name"], reference)
        request["audit"] = [
            dict(row)
            for row in conn.execute(
                """

                SELECT *

                FROM audit_log

                WHERE request_id = ?

                ORDER BY id

                """,
                (request["id"],),
            )
        ]
        return request


def cancel(
    settings: Settings,
    key: str,
    reference: str,
) -> dict[str, Any]:
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        client = get_client(conn, key)
        request = _owned_request(conn, client["system_name"], reference)
        if request["status"] != "pending":
            raise ServiceError("Only pending tasks can be cancelled.", 409)
        if _expired(request["expires_at"]):
            conn.execute(
                """

                UPDATE approval_requests

                SET status = 'expired', updated_at = ?

                WHERE id = ? AND status = 'pending'

                """,
                (utc_now(), request["id"]),
            )
            audit(conn, request["id"], "expired", {})
            return {"status": "expired"}
        conn.execute(
            """

            UPDATE approval_requests

            SET status = 'cancelled', updated_at = ?

            WHERE id = ? AND status = 'pending'

            """,
            (utc_now(), request["id"]),
        )
        audit(conn, request["id"], "cancelled", {})
        return {"status": "cancelled"}
