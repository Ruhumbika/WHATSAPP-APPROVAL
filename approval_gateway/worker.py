"""Process queued deliveries and callbacks without holding network transactions."""

from __future__ import annotations
import fcntl
import argparse
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit
from .notifications import enqueue_notification, process_notifications
from .config import Settings, load_settings
from .db import connection, init_db, utc_now
from .service import (
    ServiceError,
    _check_actor,
    _expired,
    audit,
    process_workflow_event,
    verify_delivery_workflow,
)
from .workflow_events import init_workflow_events
from .integrations.bootstrap import bootstrap_integrations
from .integrations.registry import get_workflow_guard
from .integrations.permissions import IntegrationUnavailable, WorkflowDenied
from .upgrade import migrate
from .whatsapp import WhatsAppClient, WhatsAppError

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 8
MAX_CALLBACK_RESPONSE_BYTES = 65_536


class CallbackError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class _RejectRedirects(urllib.request.HTTPRedirectHandler):

    # Callback secrets must never be forwarded to redirected endpoints.

    def redirect_request(self, req, fp, code, msg, headers, new_url):
        del req, fp, code, msg, headers, new_url
        return None


def later(seconds: int) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .replace(microsecond=0)
        .isoformat()
    )


def _retry_at(attempts: int) -> str:

    # Increase delay from 30 seconds to a maximum of one hour.
    delay = min(3600, 30 * 2 ** min(max(attempts - 1, 0), 7))
    return later(delay)


@contextmanager
def _worker_lock(settings: Settings) -> Iterator[bool]:

    # Serialize cooperating workers on the same Linux host.
    lock_path = Path(settings.database_path + ".worker.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _recover_interrupted_work(settings: Settings) -> None:
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")

        # An interrupted send may have reached Meta; do not automatically resend.
        interrupted = conn.execute("""
            SELECT id
            FROM approval_requests
            WHERE delivery_status = 'sending'
            """).fetchall()
        for row in interrupted:
            conn.execute(
                """
                UPDATE approval_requests
                SET delivery_status = 'unknown',
                    delivery_next_at = NULL,
                    delivery_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    "Worker interrupted; message acceptance requires review.",
                    utc_now(),
                    row["id"],
                ),
            )
            audit(conn, row["id"], "delivery_unknown", {})

        # Event task creation is atomic; a recovered trigger can be resolved again.
        conn.execute(
            """UPDATE workflow_events
               SET status = 'pending', next_attempt_at = ?, updated_at = ?
               WHERE status = 'processing'""",
            (utc_now(), utc_now()),
        )

        # Callback retries are safe only when the ERP deduplicates event_id.
        conn.execute(
            """
            UPDATE callback_attempts
            SET status = 'pending',
                next_attempt_at = ?,
                updated_at = ?
            WHERE status = 'processing'
            """,
            (utc_now(), utc_now()),
        )


def _claim_delivery(settings: Settings) -> dict[str, Any] | None:
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = utc_now()

        # Expire queued tasks before selecting work.
        expired = conn.execute(
            """
            SELECT id
            FROM approval_requests
            WHERE status = 'pending'
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            """,
            (now,),
        ).fetchall()
        for row in expired:
            conn.execute(
                """
                UPDATE approval_requests
                SET status = 'expired', updated_at = ?
                WHERE id = ?
                """,
                (now, row["id"]),
            )
            audit(conn, row["id"], "expired", {})
        row = conn.execute(
            """
            SELECT *
            FROM approval_requests
            WHERE status = 'pending'
              AND delivery_status = 'pending'
              AND delivery_next_at <= ?
            ORDER BY delivery_next_at, id
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if row is None:
            return None
        request = dict(row)
        attempts = request["delivery_attempts"] + 1

        # Count the attempt before I/O so crashes cannot erase attempt history.
        conn.execute(
            """
            UPDATE approval_requests
            SET delivery_status = 'sending',
                delivery_attempts = ?,
                delivery_next_at = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (attempts, now, request["id"]),
        )
        request["delivery_attempts"] = attempts
        return request


def _prepare_delivery(
    settings: Settings,
    request_id: int,
) -> dict[str, Any]:
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise ServiceError("Task is missing.", 404)
        request = dict(row)
        if (
            request["status"] != "pending"
            or request["delivery_status"] != "sending"
        ):
            raise ServiceError("Task is no longer eligible for delivery.", 409)
        if _expired(request["expires_at"]):
            conn.execute(
                """
                UPDATE approval_requests
                SET status = 'expired', updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), request_id),
            )
            audit(conn, request_id, "expired", {})
            request["status"] = "expired"
        else:
            _check_actor(conn, request, request["actor_phone"])

    # Raise after committing the expiry update.
    if request["status"] == "expired":
        raise ServiceError("Task expired.", 409)
    verify_delivery_workflow(settings, request)
    return request


def _finish_delivery(
    settings: Settings,
    request: dict[str, Any],
    status: str,
    *,
    message_id: str | None = None,
    error: str | None = None,
) -> None:
    next_at = (
        _retry_at(request["delivery_attempts"])
        if status == "pending"
        else None
    )
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE approval_requests
            SET delivery_status = ?,
                whatsapp_message_id = COALESCE(?, whatsapp_message_id),
                delivery_error = ?,
                delivery_next_at = ?,
                updated_at = ?
            WHERE id = ? AND delivery_status = 'sending'
            """,
            (
                status,
                message_id,
                error,
                next_at,
                utc_now(),
                request["id"],
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Delivery claim was lost.")
        audit(
            conn,
            request["id"],
            "delivery_result",
            {"status": status, "message_id": message_id},
        )


def _send_delivery(
    settings: Settings,
    client: WhatsAppClient,
    claimed: dict[str, Any],
) -> None:
    try:
        request = _prepare_delivery(settings, claimed["id"])
        if request["delivery_attempts"] > MAX_ATTEMPTS:
            raise ValueError("Delivery attempt limit reached.")
        adapter = get_workflow_guard(request["source_system"])
        preparer = getattr(adapter, "prepare_delivery", None)
        details = (
            preparer(settings, request)
            if callable(preparer)
            else json.loads(request["payload_json"])
        )

        # Recheck after document I/O; no message is sent for a changed ERP snapshot.
        verify_delivery_workflow(settings, request)
        if not isinstance(details, dict):
            raise ValueError("Stored payload is not an object.")

        # Reference is authoritative; display fields must come from the snapshot.
        details["reference_id"] = request["source_reference_id"]
    except IntegrationUnavailable:
        retry = claimed["delivery_attempts"] < MAX_ATTEMPTS
        _finish_delivery(
            settings,
            claimed,
            "pending" if retry else "failed",
            error="Integration delivery preparation is unavailable.",
        )
        return
    except WorkflowDenied:
        _finish_delivery(
            settings,
            claimed,
            "blocked",
            error="Integration delivery preparation was denied.",
        )
        return
    except ServiceError as exc:
        retry = (
            exc.status >= 500 and claimed["delivery_attempts"] < MAX_ATTEMPTS
        )
        _finish_delivery(
            settings,
            claimed,
            "pending" if retry else "blocked",
            error=(
                "Workflow verification unavailable."
                if retry
                else "Task no longer eligible for delivery."
            ),
        )
        return
    except ValueError:
        _finish_delivery(
            settings,
            claimed,
            "blocked",
            error="Task or template data failed delivery preparation.",
        )
        return
    try:
        message_id = client.send_approval_request(
            request["actor_phone"],
            request["company"],
            request["message"],
            request["gateway_reference_id"],
            details,
        )
    except ValueError:
        _finish_delivery(
            settings,
            claimed,
            "blocked",
            error="Recipient or template parameters are invalid.",
        )
        return
    except WhatsAppError as exc:
        if exc.delivery_unknown:
            status = "unknown"
        elif (
            exc.http_status == 429
            and claimed["delivery_attempts"] < MAX_ATTEMPTS
        ):
            status = "pending"
        else:
            status = "failed"
        _finish_delivery(
            settings,
            claimed,
            status,
            error=(
                f"WhatsApp request failed: HTTP {exc.http_status}, "
                f"API code {exc.api_code}."
            ),
        )
        return
    except Exception:

        # Unexpected failures after entering the send path require review.
        _finish_delivery(
            settings,
            claimed,
            "unknown",
            error="Unexpected send failure; acceptance requires review.",
        )
        logger.error("WhatsApp send encountered an unexpected failure.")
        return
    _finish_delivery(
        settings,
        claimed,
        "simulated" if settings.dry_run_whatsapp else "sent",
        message_id=message_id,
    )


def _claim_callback(settings: Settings) -> dict[str, Any] | None:
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")

        # LEFT JOIN keeps missing integration configuration visible as an error.
        row = conn.execute(
            """
            SELECT ca.*,
                   ar.source_system,
                   i.callback_url AS registered_callback_url,
                   i.callback_secret,
                   ac.active AS client_active
            FROM callback_attempts AS ca
            JOIN approval_requests AS ar
              ON ar.id = ca.approval_request_id
            LEFT JOIN integrations AS i
              ON i.system_name = ar.source_system
            LEFT JOIN api_clients AS ac
              ON ac.system_name = ar.source_system
            WHERE ca.status = 'pending'
              AND ca.next_attempt_at <= ?
            ORDER BY ca.next_attempt_at, ca.id
            LIMIT 1
            """,
            (utc_now(),),
        ).fetchone()
        if row is None:
            return None
        callback = dict(row)
        callback["attempts"] += 1
        conn.execute(
            """
            UPDATE callback_attempts
            SET status = 'processing',
                attempts = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (callback["attempts"], utc_now(), callback["id"]),
        )
        return callback


def _post_callback(
    settings: Settings,
    callback: dict[str, Any],
) -> dict[str, Any]:
    if callback["attempts"] > MAX_ATTEMPTS:
        raise CallbackError("Callback attempt limit reached.")
    if callback["client_active"] != 1:
        raise CallbackError("Source system is disabled.")
    secret = callback["callback_secret"]
    if not isinstance(secret, str) or len(secret) < 32:
        raise CallbackError("Integration callback secret is invalid.")
    if callback["callback_url"] != callback["registered_callback_url"]:
        raise CallbackError("Registered callback URL has changed.")
    try:
        url = urlsplit(callback["callback_url"])
        url.port
        local_http = (
            settings.app_env in {"local", "testing"}
            and url.scheme == "http"
            and url.hostname in {"127.0.0.1", "localhost", "::1"}
        )
        if (
            (url.scheme != "https" and not local_http)
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.fragment
        ):
            raise ValueError()
    except ValueError:
        raise CallbackError("Callback URL is invalid.") from None
    body = callback["payload_json"].encode("utf-8")
    try:
        event = json.loads(body)
    except ValueError:
        raise CallbackError("Stored callback JSON is invalid.") from None
    event_id = event.get("event_id") if isinstance(event, dict) else None
    if (
        not isinstance(event_id, str)
        or not event_id
        or len(event_id) > 200
        or any(ord(char) < 32 or ord(char) > 126 for char in event_id)
    ):
        raise CallbackError("Callback event ID is invalid.")

    # Re-sign each attempt while retaining the original event identity and body.
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    request = urllib.request.Request(
        callback["callback_url"],
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Approval-Gateway-Timestamp": timestamp,
            "X-Approval-Gateway-Signature": signature,
            "Idempotency-Key": event_id,
        },
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(
            request,
            timeout=settings.callback_timeout_seconds,
        ) as response:
            if response.status != 200:
                raise CallbackError(
                    "Callback must return HTTP 200.",
                    retryable=True,
                )
            raw = response.read(MAX_CALLBACK_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise CallbackError(
            f"Callback returned HTTP {status}.",
            retryable=status == 429 or status >= 500,
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CallbackError(
            "Callback connection failed.",
            retryable=True,
        ) from None
    if len(raw) > MAX_CALLBACK_RESPONSE_BYTES:
        raise CallbackError("Callback response is too large.", retryable=True)
    try:
        reply = json.loads(raw)
    except ValueError:
        raise CallbackError(
            "Callback response is invalid JSON.",
            retryable=True,
        ) from None
    if (
        not isinstance(reply, dict)
        or reply.get("event_id") != event_id
        or reply.get("execution_status") not in {"applied", "rejected"}
    ):
        raise CallbackError(
            "Callback did not confirm this event.",
            retryable=True,
        )
    return reply


def _send_callback(
    settings: Settings,
    callback: dict[str, Any],
) -> None:
    try:
        reply = _post_callback(settings, callback)
    except CallbackError as exc:
        retry = exc.retryable and callback["attempts"] < MAX_ATTEMPTS
        status = "pending" if retry else "failed"
        with connection(settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE callback_attempts
                SET status = ?,
                    next_attempt_at = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'processing'
                """,
                (
                    status,
                    _retry_at(callback["attempts"]),
                    str(exc),
                    utc_now(),
                    callback["id"],
                ),
            )
            audit(
                conn,
                callback["approval_request_id"],
                "callback_result",
                {"status": status, "attempt": callback["attempts"]},
            )
        return
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = utc_now()
        cursor = conn.execute(
            """
            UPDATE callback_attempts
            SET status = 'sent', last_error = NULL, updated_at = ?
            WHERE id = ? AND status = 'processing'
            """,
            (now, callback["id"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Callback claim was lost.")
        conn.execute(
            """
            UPDATE approval_requests
            SET execution_status = ?,
                executed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                reply["execution_status"],
                now,
                now,
                callback["approval_request_id"],
            ),
        )
        request = dict(
            conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?",
                (callback["approval_request_id"],),
            ).fetchone()
        )
        enqueue_notification(conn, request, "result", now)
        audit(
            conn,
            callback["approval_request_id"],
            "execution_confirmed",
            {
                "event_id": reply["event_id"],
                "execution_status": reply["execution_status"],
            },
        )


def _claim_workflow_event(settings: Settings) -> dict[str, Any] | None:

    # Claim one durable trigger under the same worker lock as deliveries and callbacks.
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = utc_now()
        row = conn.execute(
            """SELECT * FROM workflow_events
               WHERE status = 'pending' AND next_attempt_at <= ?
               ORDER BY next_attempt_at, id LIMIT 1""",
            (now,),
        ).fetchone()
        if row is None:
            return None
        event = dict(row)
        event["attempts"] += 1
        conn.execute(
            """UPDATE workflow_events SET status = 'processing', attempts = ?, updated_at = ?
               WHERE id = ? AND status = 'pending'""",
            (event["attempts"], now, event["id"]),
        )
        return event


def _process_event(settings: Settings, event: dict[str, Any]) -> None:
    try:
        process_workflow_event(settings, event["id"])
        return
    except ServiceError as exc:
        retryable = exc.status >= 500 or exc.status == 409
        reason = (
            f"Workflow event processing failed "
            f"(HTTP {exc.status}): {str(exc)[:300]}"
            if isinstance(exc, ServiceError)
            else f"Workflow event processing failed: {str(exc)[:300]}"
        )
        logger.warning("Workflow event %s: %s", event["id"], reason)
    except Exception:
        # Persist a bounded failure result without recording contacts or payload contents.
        retryable = True
        reason = "Unexpected workflow event processing failure."
        logger.error("Workflow event processing failed.")
    retry = retryable and event["attempts"] < MAX_ATTEMPTS
    with connection(settings) as conn:
        conn.execute(
            """UPDATE workflow_events
              SET status = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
              WHERE id = ? AND status = 'processing' AND attempts = ?""",
            (
                "pending" if retry else "failed",
                _retry_at(event["attempts"]),
                reason,
                utc_now(),
                event["id"],
                event["attempts"],
            ),
        )


def run_once(settings: Settings, limit: int = 25) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100.")
    init_workflow_events(settings)
    with _worker_lock(settings) as acquired:
        if not acquired:
            return {
                "skipped": "worker_already_running",
                "workflow_events": 0,
                "delivery_attempts": 0,
                "callback_attempts": 0,
            }
        _recover_interrupted_work(settings)
        client = WhatsAppClient(settings)
        events = deliveries = callbacks = 0

        # Resolve triggers before sending the tasks they create.
        for _ in range(limit):
            event = _claim_workflow_event(settings)
            if event is None:
                break
            events += 1
            _process_event(settings, event)
        for _ in range(limit):
            request = _claim_delivery(settings)
            if request is None:
                break
            deliveries += 1
            _send_delivery(settings, client, request)
        for _ in range(limit):
            callback = _claim_callback(settings)
            if callback is None:
                break
            callbacks += 1
            _send_callback(settings, callback)
        process_notifications(settings, client, limit)
        return {
            "workflow_events": events,
            "delivery_attempts": deliveries,
            "callback_attempts": callbacks,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process durable workflow events and approval queues."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Continue processing until stopped.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=2,
        help="Seconds between batches in loop mode.",
    )
    args = parser.parse_args()
    if not 1 <= args.interval <= 300:
        parser.error("interval must be between 1 and 300 seconds")
    settings = load_settings()

    # The worker is a separate process and must initialize its own adapter registry.
    bootstrap_integrations(settings)
    init_db(settings)
    migrate(settings)
    init_workflow_events(settings)
    try:
        while True:
            print(json.dumps(run_once(settings)), flush=True)
            if not args.loop:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Approval worker stopping.")


if __name__ == "__main__":
    main()
