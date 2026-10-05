from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from approval_gateway.callbacks import verify_callback
from approval_gateway.config import Settings
from approval_gateway.db import connection, init_db
from approval_gateway.meta import extract_responses
from approval_gateway.repository import create_api_client, upsert_approver
from approval_gateway.service import (
    ServiceError,
    cancel,
    create,
    decide,
    read_request,
)
from approval_gateway.upgrade import migrate
from approval_gateway.whatsapp import WhatsAppError
from approval_gateway.worker import run_once


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        # Tests use isolated storage and never load the developer's .env.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)

        self.settings = Settings(
            app_env="testing",
            host="127.0.0.1",
            port=8088,
            database_path=str(root / "gateway.sqlite3"),
            log_path=str(root / "gateway.log"),
            public_base_url="http://127.0.0.1:8088",
            default_expiry_hours=24,
            callback_timeout_seconds=2,
            callback_secret="",
            meta_verify_token="test-verify-token",
            meta_app_secret="test-app-secret",
            whatsapp_access_token="test-access-token",
            whatsapp_phone_number_id="123",
            whatsapp_api_version="v23.0",
            whatsapp_template_name="workflow_approval_document",
            whatsapp_template_language="en",
            dry_run_whatsapp=True,
        )

        self.actor_uuid = "0656c52c-970d-11f1-b365-6805cae19a58"
        self.phone = "255787550399"
        self.secret = "s" * 32

        init_db(self.settings)
        migrate(self.settings)

        # System identity, company scope and local user mapping are distinct.
        with connection(self.settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            create_api_client(conn, "peta", "test-peta-key")
            create_api_client(conn, "other", "test-other-key")

            upsert_approver(
                conn,
                "John John",
                "Requisition reviewer",
                "77",
                self.phone,
            )

            approver_id = conn.execute("SELECT id FROM approvers").fetchone()[
                "id"
            ]

            conn.execute(
                """
                INSERT INTO actor_bindings (
                    system_name, company, directory_uuid,
                    source_user_id, approver_id, active
                )
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                ("peta", "77", self.actor_uuid, "42", approver_id),
            )

            conn.execute(
                """
                INSERT INTO integrations (
                    system_name, callback_url, callback_secret
                )
                VALUES (?, ?, ?)
                """,
                ("peta", "http://127.0.0.1:1/callback", self.secret),
            )

        self.data = {
            "request_type": "requisition_approval",
            "company": "77",
            "reference_id": "REQ-9",
            "message": "Review workshop materials.",
            "step_id": "finance_verification",
            "workflow_version": "1",
            "actor_directory_uuid": self.actor_uuid,
            "payload": {
                "document_media_id": "987",
                "document_filename": "REQ-9.pdf",
                "workflow_step": "Finance verification",
                "requested_by": "Daniel Hussein",
                "details": "Workshop materials",
                "decision_scope": "All eligible items",
            },
        }

    def new_request(self):
        return create(
            self.settings,
            "test-peta-key",
            self.data,
        )[0]

    def response(self, request, **changes):
        result = {
            "action": "approved",
            "reference": request["gateway_reference_id"],
            "context_id": None,
            "phone": self.phone,
            "message_id": "incoming-1",
            "text": "Approve",
        }
        result.update(changes)
        return result

    def read(self, request):
        return read_request(
            self.settings,
            "test-peta-key",
            request["gateway_reference_id"],
        )

    def test_identical_request_returns_existing_task(self):
        first = self.new_request()
        second, created = create(self.settings, "test-peta-key", self.data)

        self.assertFalse(created)
        self.assertEqual(first["id"], second["id"])

    def test_changed_content_conflicts_without_new_version(self):
        self.new_request()

        with self.assertRaises(ServiceError) as caught:
            create(
                self.settings,
                "test-peta-key",
                dict(self.data, message="Changed request"),
            )

        self.assertEqual(caught.exception.status, 409)

    def test_changed_transport_key_does_not_duplicate_task(self):
        first = self.new_request()
        second, created = create(
            self.settings,
            "test-peta-key",
            dict(self.data, idempotency_key="different-transport-key"),
        )

        self.assertFalse(created)
        self.assertEqual(first["id"], second["id"])

    def test_company_without_actor_binding_is_denied(self):
        with self.assertRaises(ServiceError) as caught:
            create(
                self.settings,
                "test-peta-key",
                dict(self.data, company="88"),
            )

        self.assertEqual(caught.exception.status, 403)

    def test_other_system_cannot_read_task(self):
        request = self.new_request()

        with self.assertRaises(ServiceError) as caught:
            read_request(
                self.settings,
                "test-other-key",
                request["gateway_reference_id"],
            )

        self.assertEqual(caught.exception.status, 404)

    def test_unregistered_callback_is_denied(self):
        with self.assertRaises(ServiceError) as caught:
            create(
                self.settings,
                "test-peta-key",
                dict(
                    self.data, callback_url="https://invalid.example/callback"
                ),
            )

        self.assertEqual(caught.exception.status, 403)

    def test_wrong_sender_cannot_decide(self):
        request = self.new_request()

        with self.assertRaises(ServiceError):
            decide(
                self.settings,
                self.response(request, phone="255711111111"),
            )

        self.assertEqual(self.read(request)["status"], "pending")

    def test_changed_binding_blocks_decision(self):
        request = self.new_request()

        with connection(self.settings) as conn:
            conn.execute("UPDATE actor_bindings SET source_user_id = '99'")

        with self.assertRaises(ServiceError):
            decide(self.settings, self.response(request))

        self.assertEqual(self.read(request)["status"], "pending")

    def test_expired_task_has_no_callback(self):
        request = self.new_request()

        with connection(self.settings) as conn:
            conn.execute(
                """
                UPDATE approval_requests
                SET expires_at = ?
                WHERE id = ?
                """,
                ("2000-01-01T00:00:00+00:00", request["id"]),
            )

        result = decide(self.settings, self.response(request))

        self.assertEqual(result["reason"], "expired")

        with connection(self.settings) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM callback_attempts"
            ).fetchone()[0]

        self.assertEqual(count, 0)

    def test_duplicate_decision_creates_one_callback(self):
        request = self.new_request()

        first = decide(self.settings, self.response(request))
        duplicate = decide(self.settings, self.response(request))

        self.assertTrue(first["processed"])
        self.assertEqual(duplicate["reason"], "duplicate_message")

        with connection(self.settings) as conn:
            callbacks = conn.execute(
                "SELECT payload_json FROM callback_attempts"
            ).fetchall()

        self.assertEqual(len(callbacks), 1)
        event = json.loads(callbacks[0]["payload_json"])
        self.assertEqual(event["actor"]["directory_uuid"], self.actor_uuid)
        self.assertEqual(event["actor"]["user_id"], "42")
        self.assertEqual(event["company"], "77")
        self.assertEqual(self.read(request)["execution_status"], "pending")

    def test_cancel_blocks_decision_and_next_step_is_distinct(self):
        request = self.new_request()
        cancel(
            self.settings,
            "test-peta-key",
            request["gateway_reference_id"],
        )

        result = decide(self.settings, self.response(request))
        self.assertEqual(result["reason"], "cancelled")

        next_request, created = create(
            self.settings,
            "test-peta-key",
            dict(self.data, step_id="director_approval"),
        )

        self.assertTrue(created)
        self.assertNotEqual(request["id"], next_request["id"])

    def test_text_reply_resolves_task_from_context(self):
        request = self.new_request()

        with connection(self.settings) as conn:
            conn.execute(
                """
                UPDATE approval_requests
                SET whatsapp_message_id = ?
                WHERE id = ?
                """,
                ("outgoing-1", request["id"]),
            )

        result = decide(
            self.settings,
            self.response(
                request,
                reference=None,
                context_id="outgoing-1",
            ),
        )

        self.assertTrue(result["processed"])

    def test_parser_filters_account_and_ambiguous_text(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "123"},
                                "messages": [
                                    {
                                        "type": "text",
                                        "id": "incoming-a",
                                        "from": self.phone,
                                        "text": {"body": "go on"},
                                        "context": {"id": "outgoing-1"},
                                    },
                                    {
                                        "type": "interactive",
                                        "id": "incoming-b",
                                        "from": self.phone,
                                        "interactive": {
                                            "type": "button_reply",
                                            "button_reply": {
                                                "id": "reject_AGR-ABC"
                                            },
                                        },
                                    },
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        responses = list(
            extract_responses(
                payload,
                expected_phone_number_id="123",
            )
        )

        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["action"], "rejected")
        self.assertEqual(
            list(
                extract_responses(
                    payload,
                    expected_phone_number_id="999",
                )
            ),
            [],
        )

    def test_unknown_delivery_is_not_automatically_retried(self):
        request = self.new_request()

        with patch(
            "approval_gateway.worker.WhatsAppClient.send_approval_request",
            side_effect=WhatsAppError(
                "Acceptance unknown.",
                delivery_unknown=True,
            ),
        ) as send:
            run_once(self.settings)
            run_once(self.settings)

        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.read(request)["delivery_status"], "unknown")

    def test_rate_limit_schedules_retry(self):
        request = self.new_request()

        with patch(
            "approval_gateway.worker.WhatsAppClient.send_approval_request",
            side_effect=WhatsAppError("Rate limited.", http_status=429),
        ):
            run_once(self.settings)

        updated = self.read(request)
        self.assertEqual(updated["delivery_status"], "pending")
        self.assertEqual(updated["delivery_attempts"], 1)
        self.assertIsNotNone(updated["delivery_next_at"])

    def test_signed_callback_confirms_execution(self):
        # A loopback receiver verifies the real worker's signed HTTP request.
        received = []
        secret = self.secret

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))

                valid = verify_callback(
                    body,
                    self.headers.get("X-Approval-Gateway-Timestamp"),
                    self.headers.get("X-Approval-Gateway-Signature"),
                    secret,
                )

                if not valid:
                    self.send_response(403)
                    self.end_headers()
                    return

                event = json.loads(body)
                received.append((event, self.headers.get("Idempotency-Key")))

                reply = json.dumps(
                    {
                        "event_id": event["event_id"],
                        "execution_status": "applied",
                    }
                ).encode("utf-8")

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
        )
        thread.start()

        try:
            with connection(self.settings) as conn:
                conn.execute(
                    """
                    UPDATE integrations
                    SET callback_url = ?
                    WHERE system_name = 'peta'
                    """,
                    (f"http://127.0.0.1:{server.server_port}/callback",),
                )

            request = self.new_request()
            decide(self.settings, self.response(request))
            run_once(self.settings)

            self.assertEqual(
                self.read(request)["execution_status"],
                "applied",
            )
            self.assertEqual(len(received), 1)
            self.assertEqual(
                received[0][0]["event_id"],
                received[0][1],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_migration_rerun_preserves_existing_task(self):
        request = self.new_request()

        migrate(self.settings)
        migrate(self.settings)

        updated = self.read(request)
        self.assertEqual(updated["id"], request["id"])
        self.assertEqual(updated["actor_directory_uuid"], self.actor_uuid)


if __name__ == "__main__":
    unittest.main()
