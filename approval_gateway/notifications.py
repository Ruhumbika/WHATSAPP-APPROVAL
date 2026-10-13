"""Durable decision confirmations, independent of ERP execution and delivery."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import sqlite3
from typing import Any

from .config import Settings
from .db import connection, utc_now
from .whatsapp import WhatsAppClient, WhatsAppError

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


def _company_name(task: dict[str, Any]) -> str:
    # PETA resolves company_id against companies before storing this snapshot.
    company = f"Company {task['company']}"
    try:
        snapshot = json.loads(task["payload_json"]).get("record_snapshot", {})
        name = snapshot.get("company_name")
        if (
            str(snapshot.get("company_id")) == str(task["company"])
            and isinstance(name, str)
            and name.strip()
            and len(name) <= 200
        ):
            company = " ".join(name.split())
    except (ValueError, TypeError, AttributeError):
        pass
    return company


def enqueue_notification(
    conn: sqlite3.Connection,
    task: dict[str, Any],
    kind: str,
    now: str,
    *,
    decision: str | None = None,
) -> None:
    # The verified button reply opens the service window. Never backfill old tasks.
    responded_at = now if kind == "received" else task.get("responded_at")
    if not responded_at:
        return
    expires = (
        datetime.fromisoformat(responded_at) + timedelta(hours=23, minutes=55)
    ).isoformat()
    if expires <= now:
        return
    action = decision or task["status"]
    if action not in {"approved", "rejected"}:
        return
    reference = task["source_reference_id"]
    company = _company_name(task)
    label = (
        "Payment Advice"
        if task.get("request_type") == "payment_advice_approval"
        else "requisition"
    )
    rejection = json.loads(task.get("decision_rejection_json") or "null")
    verb = "approve" if action == "approved" else "reject"
    if action == "rejected" and rejection:
        verb = (
            "return for correction"
            if rejection["mode"] == "correction"
            else "reject completely"
        )
    if kind == "received":
        if rejection and rejection["mode"] == "correction":
            body = f"{company}\nYour request to return {label} {reference} for correction has been submitted. Waiting for PETA confirmation."
        else:
            body = f"{company}\nYour decision to {verb} {label} {reference} has been submitted. Waiting for PETA confirmation."
    elif kind == "result":
        if task["execution_status"] == "applied":
            if rejection:
                if rejection["mode"] == "correction":
                    body = f"{company}\nPETA returned {label} {reference} for correction.\nReason: {rejection['reason']}\nCorrect it in PETA and resubmit. A new approval cycle will start."
                else:
                    body = f"{company}\nPETA rejected {label} {reference}.\nReason: {rejection['reason']}"
            else:
                body = f"{company}\nYour decision to {verb} {label} {reference} has been successfully applied at your assigned approval step."
        elif task["execution_status"] == "rejected":
            body = f"{company}\nYour decision for {label} {reference} was not applied because the workflow did not accept this action. Please review the requisition in {company}."
        else:
            return
    else:
        raise ValueError("Invalid notification kind")
    conn.execute(
        """
        INSERT INTO decision_notifications
            (approval_request_id, kind, phone, body, next_attempt_at,
            expires_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(approval_request_id, kind) DO NOTHING
    """,
        (task["id"], kind, task["actor_phone"], body, now, expires, now, now),
    )



def enqueue_execution_failure(conn, task, now):
    if not task.get('responded_at'):
        return
    expires = (datetime.fromisoformat(task['responded_at']) + timedelta(hours=23, minutes=55)).isoformat()
    if expires <= now:
        return
    body = (f"{_company_name(task)}\nCould not confirm your decision for request {task['source_reference_id']} with PETA. "
            "Contact support to check its status before taking another action.")
    conn.execute("""INSERT INTO decision_repeat_notifications
        (approval_request_id,inbound_message_id,kind,phone,body,next_attempt_at,expires_at,created_at,updated_at)
        VALUES (?,?,'received',?,?,?,?,?,?) ON CONFLICT(inbound_message_id) DO NOTHING""",
        (task['id'], 'callback-failure-' + str(task['id']), task['actor_phone'], body, now, expires, now, now))


def enqueue_repeat_notification(conn, task, response, now):
    if task["status"] not in {"approved", "rejected"}:
        return
    company = _company_name(task)
    label = (
        "Payment Advice"
        if task.get("request_type") == "payment_advice_approval"
        else "requisition"
    )
    first = "approve" if task["status"] == "approved" else "reject"
    attempted = "approve" if response["action"] == "approved" else "reject"
    prefix = f"{company}\nYou cannot {attempted} requisition {task['source_reference_id']}"
    if task["execution_status"] == "applied":
        body = f"{prefix} because your decision to {first} it has already been applied at your assigned approval step."
    elif task["execution_status"] == "pending":
        failed = conn.execute("SELECT 1 FROM callback_attempts WHERE approval_request_id=? AND status='failed'", (task['id'],)).fetchone()
        body = (f"{prefix} because your decision is already recorded. Could not confirm it with PETA; contact support."
                if failed else f"{prefix} because your decision to {first} it has already been recorded. Waiting for PETA confirmation.")
    else:
        body = f"{prefix} because your decision to {first} it has already been recorded. It was not applied; please review the requisition in {company}."
    expires = (
        datetime.fromisoformat(now) + timedelta(hours=23, minutes=55)
    ).isoformat()
    conn.execute(
        """INSERT INTO decision_repeat_notifications
        (approval_request_id, inbound_message_id, kind, phone, body,
        next_attempt_at, expires_at, created_at, updated_at)
        VALUES (?, ?, 'received', ?, ?, ?, ?, ?, ?)
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
        ),
    )


def process_notifications(
    settings: Settings, client: WhatsAppClient, limit: int
) -> None:
    # Called only under the existing exclusive worker lock.
    for table in ("decision_notifications", "decision_repeat_notifications"):
        _process_queue(settings, client, limit, table)


def _process_queue(settings, client, limit, table):
    if table not in {
        "decision_notifications",
        "decision_repeat_notifications",
    }:
        raise ValueError("Invalid notification table")
    with connection(settings) as conn:
        conn.execute(
            f"""UPDATE {table}
            SET status='unknown', last_error='Interrupted send; acceptance requires review.', updated_at=?
            WHERE status='sending'""",
            (utc_now(),),
        )
    for _ in range(limit):
        with connection(settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            conn.execute(
                f"""UPDATE {table}
                SET status='expired', updated_at=?
                WHERE status='pending' AND expires_at<=?""",
                (now, now),
            )
            row = conn.execute(
                f"""
                SELECT n.* FROM {table} n
                WHERE n.status='pending' AND n.next_attempt_at<=?
                  AND (n.kind='received' OR NOT EXISTS (
                      SELECT 1 FROM {table} first
                      WHERE first.approval_request_id=n.approval_request_id
                        AND first.kind='received' AND first.status IN ('pending','sending')
                  ))
                ORDER BY n.id LIMIT 1
            """,
                (now,),
            ).fetchone()
            if row is None:
                return
            notification = dict(row)
            attempts = notification["attempts"] + 1
            conn.execute(
                f"""UPDATE {table}
                SET status='sending', attempts=?, updated_at=? WHERE id=?""",
                (attempts, now, notification["id"]),
            )
        from .service import ServiceError
        status, message_id, error = "sent", None, None
        try:
            if notification.get("payload_json"):
                from .progress import prepare_progress, template_parameters
                payload = json.loads(notification["payload_json"])
                if payload.get("type") == "rejection_choice":
                    message_id = client.send_rejection_choices(notification["phone"], notification["body"], payload["reference"])
                elif payload.get("type") == "rejection_review":
                    with connection(settings) as conn:
                        current = conn.execute("SELECT * FROM approval_requests WHERE id=?", (notification["approval_request_id"],)).fetchone()
                    if current["status"] != "pending" or current["rejection_phase"] != "reason" or current["rejection_review_token"] != payload["token"] or current["rejection_expires_at"] <= utc_now():
                        raise ValueError("Rejection review superseded")
                    message_id = client.send_rejection_review(notification["phone"], notification["body"], payload["token"])
                elif payload.get("type") == "progress" and payload["response"]["phone"] == notification["phone"]:
                    task, data, body = prepare_progress(settings, payload["response"])
                    if payload["response"].get("action") == "progress":
                        message_id = client.send_text(notification["phone"], body)
                    else:
                        message_id = client.send_progress(notification["phone"], task["gateway_reference_id"], template_parameters(data))
                else:
                    raise ValueError("Invalid queued notification.")
            else:
                message_id = client.send_text(notification["phone"], notification["body"])
            if not isinstance(message_id, str) or not message_id:
                message_id = None
                raise WhatsAppError(
                    "Missing acknowledgement", delivery_unknown=True
                )
            if settings.dry_run_whatsapp:
                status = "simulated"
        except WhatsAppError as exc:
            if exc.delivery_unknown:
                status = "unknown"
            elif exc.http_status == 429 and attempts < MAX_ATTEMPTS:
                status = "pending"
            else:
                status = "failed"
            error = f"WhatsApp confirmation failed: HTTP {exc.http_status}, API code {exc.api_code}."
        except ServiceError as exc:
            status = "pending" if exc.status >= 500 and attempts < MAX_ATTEMPTS else "failed"
            error = f"Progress verification failed: HTTP {exc.status}."
        except ValueError:
            status, error = "failed", "Invalid confirmation recipient or text."
        except Exception:
            status, error = (
                "unknown",
                "Unexpected send failure; acceptance requires review.",
            )
            logger.error("Decision confirmation send failed.")
        next_at = (
            datetime.fromisoformat(utc_now())
            + timedelta(seconds=min(600, 30 * 2 ** (attempts - 1)))
        ).isoformat()
        with connection(settings) as conn:
            conn.execute(
                f"""UPDATE {table}
                SET status=?, message_id=?, last_error=?, next_attempt_at=?, updated_at=?
                WHERE id=? AND status='sending'""",
                (
                    status,
                    message_id,
                    error,
                    next_at,
                    utc_now(),
                    notification["id"],
                ),
            )
