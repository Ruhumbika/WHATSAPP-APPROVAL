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
    expires = (datetime.fromisoformat(responded_at) + timedelta(hours=23, minutes=55)).isoformat()
    if expires <= now:
        return
    action = decision or task["status"]
    if action not in {"approved", "rejected"}:
        return
    reference = task["source_reference_id"]
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
    verb = "approve" if action == "approved" else "reject"
    if kind == "received":
        body = f"{company}\nYour decision to {verb} requisition {reference} has been received. Processing is underway."
    elif kind == "result":
        if task["execution_status"] == "applied":
            body = f"{company}\nYour decision to {verb} requisition {reference} has been successfully applied at your assigned approval step."
        elif task["execution_status"] == "rejected":
            body = f"{company}\nYour decision for requisition {reference} was not applied because the workflow did not accept this action. Please review the requisition in {company}."
        else:
            return
    else:
        raise ValueError("Invalid notification kind")
    conn.execute("""
        INSERT INTO decision_notifications
            (approval_request_id, kind, phone, body, next_attempt_at,
             expires_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(approval_request_id, kind) DO NOTHING
    """, (task["id"], kind, task["actor_phone"], body, now, expires, now, now))


def process_notifications(settings: Settings, client: WhatsAppClient, limit: int) -> None:
    # Called only under the existing exclusive worker lock.
    with connection(settings) as conn:
        conn.execute("""UPDATE decision_notifications
            SET status='unknown', last_error='Interrupted send; acceptance requires review.', updated_at=?
            WHERE status='sending'""", (utc_now(),))
    for _ in range(limit):
        with connection(settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            conn.execute("""UPDATE decision_notifications
                SET status='expired', updated_at=?
                WHERE status='pending' AND expires_at<=?""", (now, now))
            row = conn.execute("""
                SELECT n.* FROM decision_notifications n
                WHERE n.status='pending' AND n.next_attempt_at<=?
                  AND (n.kind='received' OR NOT EXISTS (
                      SELECT 1 FROM decision_notifications first
                      WHERE first.approval_request_id=n.approval_request_id
                        AND first.kind='received' AND first.status IN ('pending','sending')
                  ))
                ORDER BY n.id LIMIT 1
            """, (now,)).fetchone()
            if row is None:
                return
            notification = dict(row)
            attempts = notification["attempts"] + 1
            conn.execute("""UPDATE decision_notifications
                SET status='sending', attempts=?, updated_at=? WHERE id=?""",
                (attempts, now, notification["id"]))
        status, message_id, error = "sent", None, None
        try:
            message_id = client.send_text(notification["phone"], notification["body"])
            if not isinstance(message_id, str) or not message_id:
                message_id = None
                raise WhatsAppError("Missing acknowledgement", delivery_unknown=True)
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
        except ValueError:
            status, error = "failed", "Invalid confirmation recipient or text."
        except Exception:
            status, error = "unknown", "Unexpected send failure; acceptance requires review."
            logger.error("Decision confirmation send failed.")
        next_at = (datetime.fromisoformat(utc_now()) + timedelta(seconds=min(600, 30 * 2 ** (attempts - 1)))).isoformat()
        with connection(settings) as conn:
            conn.execute("""UPDATE decision_notifications
                SET status=?, message_id=?, last_error=?, next_attempt_at=?, updated_at=?
                WHERE id=? AND status='sending'""",
                (status, message_id, error, next_at, utc_now(), notification["id"]))
