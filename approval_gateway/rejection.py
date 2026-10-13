"""Persist task-scoped rejection conversations before committing an ERP decision."""

from datetime import datetime, timedelta
import json

from .db import connection, utc_now


CHOICE_PROMPT = (
    "*How would you like to proceed?*\n\n"
    "*Return for correction*\nThe requester can edit and resubmit.\n\n"
    "*Reject completely*\nEnd this approval workflow.\n\n"
    "No decision has been submitted."
)


def _prompt(conn, task, response, body, now):
    expires = (
        datetime.fromisoformat(now) + timedelta(hours=23, minutes=55)
    ).isoformat()
    label = "Payment Advice" if task["request_type"] == "payment_advice_approval" else "Requisition"
    payload = json.dumps({"type": "rejection_choice", "reference": task["gateway_reference_id"]}) if body == CHOICE_PROMPT else None
    body = f"*{label} #{task['source_reference_id']}*\n\n{body}"
    conn.execute(
        "UPDATE decision_repeat_notifications SET status='expired',updated_at=? WHERE approval_request_id=? AND status='pending' AND (payload_json IS NULL OR json_extract(payload_json, '$.type')='rejection_choice')",
        (now, task["id"]),
    )
    conn.execute(
        """INSERT INTO decision_repeat_notifications
        (approval_request_id, inbound_message_id, kind, phone, body,
         next_attempt_at, expires_at, created_at, updated_at, payload_json)
        VALUES (?, ?, 'received', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(inbound_message_id) DO NOTHING""",
        (
            task["id"],
            response["message_id"],
            task["actor_phone"],
            body,
            now,
            expires,
            now,
            now,
            payload,
        ),
    )


def _resolve(settings, response, phone):
    # Resolve an explicit AGR, the original message, or a prompt sent for this task.
    with connection(settings) as conn:
        if response.get("reference"):
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE gateway_reference_id=?",
                (response["reference"],),
            ).fetchall()
        elif response.get("context_id"):
            rows = conn.execute(
                """SELECT DISTINCT r.* FROM approval_requests r
                LEFT JOIN decision_repeat_notifications n ON n.approval_request_id=r.id
                WHERE r.whatsapp_message_id=? OR (n.message_id=? AND n.phone=?) LIMIT 2""",
                (response["context_id"], response["context_id"], phone),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT r.* FROM approval_requests r
                WHERE r.actor_phone=? AND r.rejection_phase IN ('choice','reason')
                  AND r.rejection_expires_at>? AND r.status='pending' LIMIT 2""",
                (phone, utc_now()),
            ).fetchall()
        if len(rows) != 1:
            return None
        task = dict(rows[0])
        if response.get("reference") and response.get("context_id"):
            belongs = (
                task["whatsapp_message_id"] == response["context_id"]
                or conn.execute(
                    "SELECT 1 FROM decision_repeat_notifications WHERE approval_request_id=? AND message_id=? AND phone=?",
                    (task["id"], response["context_id"], phone),
                ).fetchone()
            )
            if not belongs:
                from .service import ServiceError

                raise ServiceError("Message context does not match task.", 403)
        return task


def route_response(settings, response):
    from .service import (
        ServiceError,
        _phone,
        _text,
        _preflight_decision,
        _commit_decision,
        _check_actor,
        _expired,
    )

    if not isinstance(response, dict):
        raise ServiceError("Response must be an object.")
    response = dict(response)
    if response.get("action") == "approved":
        return _commit_decision(settings, response)
    if response.get("action") not in {"rejected", "conversation"}:
        raise ServiceError("Invalid decision.")
    response["message_id"] = _text(
        response.get("message_id"), "message_id", 512
    )
    phone = _phone(response.get("phone"))
    text = response.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise ServiceError("Invalid conversation reply.")
    for field in ("reference", "context_id"):
        if response.get(field) is not None:
            response[field] = _text(response[field], field, 512)
    with connection(settings) as conn:
        if conn.execute(
            "SELECT 1 FROM inbound_messages WHERE message_id=?",
            (response["message_id"],),
        ).fetchone():
            return {"processed": False, "reason": "duplicate_message"}
    task = _resolve(settings, response, phone)
    if task is None:
        return {
            "processed": False,
            "reason": "missing_or_ambiguous_rejection_context",
        }
    if task["actor_phone"] != phone:
        raise ServiceError("Responder is not the assigned actor.", 403)
    if (
        task["request_type"] not in {"requisition_approval", "payment_advice_approval"}
        or task["status"] != "pending"
    ):
        if response["action"] == "rejected":
            return _commit_decision(
                settings,
                dict(response, reference=task["gateway_reference_id"]),
            )
        return {"processed": False, "reason": task["status"]}
    normalized = dict(
        response, reference=task["gateway_reference_id"], context_id=None
    )
    # Preserve the actual Meta reply context in callback evidence.
    normalized["conversation_context_id"] = response.get("context_id")
    preflight = _preflight_decision(settings, normalized, phone)
    final = None
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM inbound_messages WHERE message_id=?",
            (response["message_id"],),
        ).fetchone():
            return {"processed": False, "reason": "duplicate_message"}
        current = dict(
            conn.execute(
                "SELECT * FROM approval_requests WHERE id=?", (task["id"],)
            ).fetchone()
        )
        now = utc_now()
        if current["status"] != "pending" or _expired(current["expires_at"]):
            return {"processed": False, "reason": "task_no_longer_pending"}
        _check_actor(conn, current, phone)
        if preflight != (current["id"], current["request_hash"]):
            raise ServiceError(
                "Task changed during workflow verification.", 409
            )
        conversation = current
        active = (
            conversation["rejection_phase"] != "closed"
            and conversation["rejection_expires_at"] is not None
            and conversation["rejection_expires_at"] > now
        )
        command = text.strip().casefold()
        if response["action"] == "rejected":
            if not active:
                expires = (
                    datetime.fromisoformat(now)
                    + timedelta(hours=23, minutes=55)
                ).isoformat()
                conn.execute(
                    """UPDATE approval_requests
                    SET rejection_phase='choice', rejection_mode=NULL,
                        rejection_expires_at=?, rejection_updated_at=?
                    WHERE id=?""",
                    (expires, now, task["id"]),
                )
                body = CHOICE_PROMPT
            elif conversation["rejection_phase"] == "reason":
                body = "*Reason required*\n\nReply with the reason (3–2000 characters).\nSend CANCEL to keep the request pending.\n\nSending the reason submits your decision."
            else:
                body = CHOICE_PROMPT
        elif not active:
            return {
                "processed": False,
                "reason": "no_active_rejection_conversation",
            }
        elif command in {"cancel", "3"}:
            conn.execute(
                "UPDATE approval_requests SET rejection_phase='closed',rejection_updated_at=? WHERE id=?",
                (now, task["id"]),
            )
            body = "Rejection cancelled. Your approval task is still pending."
        elif conversation["rejection_phase"] == "choice":
            if command in {"1", "2"}:
                mode = "correction" if command == "1" else "final"
                conn.execute(
                    "UPDATE approval_requests SET rejection_phase='reason',rejection_mode=?,rejection_updated_at=? WHERE id=?",
                    (mode, now, task["id"]),
                )
                body = (
                    (
                        "*Return for correction*\n\n"
                        if mode == "correction"
                        else "*Reject completely*\n\n"
                    )
                    + "Reply with the reason (3–2000 characters).\nSend CANCEL to keep the request pending.\n\nSending the reason submits your decision to PETA."
                )
            else:
                body = CHOICE_PROMPT
        elif not 3 <= len(text.strip()) <= 2000 or command in {"1", "2"}:
            body = "Please write a reason of 3–2000 characters, or CANCEL."
        else:
            final = dict(
                normalized,
                action="rejected",
                rejection={
                    "mode": conversation["rejection_mode"],
                    "reason": text.strip(),
                },
            )
        if final is None:
            conn.execute(
                "INSERT INTO inbound_messages VALUES (?,?)",
                (response["message_id"], now),
            )
            _prompt(conn, current, response, body, now)
    if final is not None:
        # Recheck the live ERP workflow immediately before final atomic persistence.
        return _commit_decision(settings, final)
    return {"processed": True, "reason": "rejection_conversation"}


def consume_reason(conn, task, response, now):
    from .service import ServiceError, _phone

    rejection = response.get("rejection")
    conversation = task
    if (
        not isinstance(rejection, dict)
        or not conversation
        or conversation["rejection_phase"] != "reason"
        or not conversation["rejection_expires_at"]
        or conversation["rejection_expires_at"] <= now
        or conversation["actor_phone"] != _phone(response["phone"])
        or rejection.get("mode") != conversation["rejection_mode"]
        or not isinstance(rejection.get("reason"), str)
        or not 3 <= len(rejection["reason"].strip()) <= 2000
    ):
        raise ServiceError(
            "A current rejection choice and reason are required.", 409
        )
    conn.execute(
        "UPDATE approval_requests SET rejection_phase='closed',rejection_updated_at=? WHERE id=?",
        (now, task["id"]),
    )
