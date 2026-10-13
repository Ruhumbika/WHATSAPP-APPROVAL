"""Persist task-scoped rejection conversations before committing an ERP decision."""

from datetime import datetime, timedelta
import json
from uuid import uuid4

from .db import connection, utc_now


CHOICE_PROMPT = (
    "*How would you like to proceed?*\n\n"
    "*Return for correction*\nThe requester can edit and resubmit.\n\n"
    "*Reject completely*\nEnd this approval workflow.\n\n"
    "No decision has been submitted."
)


def _prompt(conn, task, response, body, now, review_token=None):
    expires = (
        datetime.fromisoformat(now) + timedelta(hours=23, minutes=55)
    ).isoformat()
    payload = json.dumps({"type": "rejection_choice", "reference": task["gateway_reference_id"]}) if body == CHOICE_PROMPT else None
    if review_token:
        payload = json.dumps({"type": "rejection_review", "token": review_token, "reference": task["gateway_reference_id"]})
    from .chat_menu import task_summary
    body = task_summary(task) + "\n\n" + body
    conn.execute(
        "UPDATE decision_repeat_notifications SET status='expired',updated_at=? WHERE approval_request_id=? AND status='pending' AND (payload_json IS NULL OR json_extract(payload_json, '$.type') IN ('rejection_choice','rejection_review'))",
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
        if response.get("review_token"):
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE rejection_review_token=? AND actor_phone=?",
                (response["review_token"], phone)).fetchall()
            if len(rows) != 1:
                return None
            delivered = conn.execute("""SELECT 1 FROM decision_repeat_notifications
                WHERE approval_request_id=? AND phone=? AND message_id=?
                AND status IN ('sent','simulated') AND expires_at>?
                AND json_extract(payload_json,'$.token')=?""",
                (rows[0]["id"], phone, response.get("context_id"), utc_now(), response["review_token"])).fetchone()
            if not delivered:
                from .service import ServiceError
                raise ServiceError("Review button context is unavailable. Resume the rejection.", 403)
        elif response.get("reference"):
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
                  AND r.rejection_expires_at>? AND r.status='pending' AND r.rejection_paused=0 LIMIT 2""",
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
        if response.get("review_action") == "submit":
            from .notifications import enqueue_repeat_notification
            with connection(settings) as conn:
                enqueue_repeat_notification(conn, task, dict(response, action="rejected"), utc_now())
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
        review_token = None
        review_action = response.get("review_action")
        if review_action:
            if response.get("review_token") != current["rejection_review_token"] or not active:
                raise ServiceError("Review changed or expired. Resume the rejection.", 409)
            if review_action == "submit":
                final = dict(normalized, action="rejected", rejection={
                    "mode": current["rejection_mode"], "reason": current["rejection_draft"]},
                    rejection_confirmation=current["rejection_review_token"])
            elif review_action == "edit":
                conn.execute("UPDATE approval_requests SET rejection_review_token=NULL,rejection_paused=0 WHERE id=?", (task["id"],))
                body = "*Edit reason*\nSend the replacement reason (3–2000 characters). Your previous draft is saved until replaced.\nNo decision has been submitted. MENU pauses; CANCEL cancels the rejection."
            elif review_action == "cancel":
                conn.execute("UPDATE approval_requests SET rejection_phase='closed',rejection_draft=NULL,rejection_updated_at=? WHERE id=?", (now, task["id"]))
                body = "Rejection cancelled. Request remains pending. Send MENU to return to the main menu."
            else:
                raise ServiceError("Invalid review action.", 400)
        elif response["action"] == "rejected":
            conn.execute("UPDATE approval_requests SET rejection_paused=0 WHERE id=?", (task["id"],))
            if not active:
                expires = (
                    datetime.fromisoformat(now)
                    + timedelta(hours=23, minutes=55)
                ).isoformat()
                conn.execute(
                    """UPDATE approval_requests
                    SET rejection_phase='choice', rejection_mode=NULL, rejection_draft=NULL, rejection_review_token=NULL, rejection_paused=0,
                        rejection_expires_at=?, rejection_updated_at=?
                    WHERE id=?""",
                    (expires, now, task["id"]),
                )
                body = CHOICE_PROMPT
            elif conversation["rejection_review_token"]:
                body = review_body(conversation)
                review_token = conversation["rejection_review_token"]
            elif conversation["rejection_phase"] == "reason":
                body = reason_body(conversation)
            else:
                body = CHOICE_PROMPT
        elif not active:
            return {
                "processed": False,
                "reason": "no_active_rejection_conversation",
            }
        elif command in {"cancel", "3"}:
            conn.execute(
                "UPDATE approval_requests SET rejection_phase='closed',rejection_draft=NULL,rejection_updated_at=? WHERE id=?",
                (now, task["id"]),
            )
            body = "Rejection cancelled. Request remains pending. Send MENU to return to the main menu."
        elif conversation["rejection_phase"] == "choice":
            if command in {"1", "2"}:
                mode = "correction" if command == "1" else "final"
                conn.execute(
                    "UPDATE approval_requests SET rejection_phase='reason',rejection_mode=?,rejection_updated_at=? WHERE id=?",
                    (mode, now, task["id"]),
                )
                current['rejection_mode'] = mode
                body = reason_body(current)
            else:
                body = CHOICE_PROMPT
        elif not 3 <= len(text.strip()) <= 2000 or command in {"1", "2"}:
            body = "Please write a reason of 3–2000 characters, or CANCEL."
        else:
            review_token = "RJC-" + uuid4().hex.upper()
            draft = text.strip()
            conn.execute("""UPDATE approval_requests SET rejection_draft=?,rejection_review_token=?,
                rejection_paused=0,rejection_updated_at=? WHERE id=?""", (draft, review_token, now, task["id"]))
            body = review_body(dict(current, rejection_draft=draft))
        if final is None:
            conn.execute(
                "INSERT INTO inbound_messages VALUES (?,?)",
                (response["message_id"], now),
            )
            _prompt(conn, current, response, body, now, review_token)
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
        or not conversation["rejection_review_token"]
        or response.get("rejection_confirmation") != conversation["rejection_review_token"]
        or rejection.get("reason") != conversation["rejection_draft"]
        or rejection.get("mode") != conversation["rejection_mode"]
        or not isinstance(rejection.get("reason"), str)
        or not 3 <= len(rejection["reason"].strip()) <= 2000
    ):
        raise ServiceError(
            "A current rejection choice and reason are required.", 409
        )
    conn.execute(
        "UPDATE approval_requests SET rejection_phase='closed',rejection_draft=NULL,rejection_updated_at=? WHERE id=?",
        (now, task["id"]),
    )


def reason_body(task):
    mode = "Return for correction" if task["rejection_mode"] == "correction" else "Reject completely"
    previous = f"\n\nPrevious draft:\n{task['rejection_draft']}" if task.get('rejection_draft') else ""
    return (f"*{mode}*\nWrite a reason (3–2000 characters). You will review it before submitting.\n"
            "MENU pauses; HELP explains this step; CANCEL keeps the request pending." + previous)


def review_body(task):
    mode = "Return for correction" if task["rejection_mode"] == "correction" else "Reject completely"
    # The full draft is delivered as text; review buttons follow in the same service window.
    return (f"*REVIEW REJECTION*\nAction: {mode}\n\nReason:\n{task['rejection_draft']}\n\n"
            "No decision has been submitted. Choose Submit rejection to confirm, or Edit reason. "
            "Cancel rejection keeps the request pending. MENU pauses and saves this draft.")
