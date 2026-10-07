"""Exercise durable workflow events, live assignment checks and signed callbacks locally."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from approval_gateway.notifications import enqueue_notification, process_notifications
from approval_gateway.whatsapp import WhatsAppError
from approval_gateway.config import load_settings
from approval_gateway.db import connection, init_db
from approval_gateway.integrations.permissions import IntegrationUnavailable
from approval_gateway.integrations.peta import (
    PetaClient,
    PetaDirectoryClient,
    PetaWorkflowGuard,
)
from approval_gateway.integrations.registry import configure_workflow_guards
from approval_gateway.repository import create_api_client
from approval_gateway.server import Server
from approval_gateway.service import (
    ServiceError,
    create,
    decide,
    process_workflow_event,
    read_request,
)
from approval_gateway.upgrade import migrate
from approval_gateway.worker import _worker_lock, run_once
from approval_gateway.workflow_events import (
    accept_workflow_event,
    init_workflow_events,
)

ACTORS = (
    ("0656c52c-970d-11f1-b365-6805cae19a58", 152, "john.john", "255787550399"),
    (
        "18958e18-4f60-11f1-8fd1-6805cae19a58",
        4,
        "daniel.hussein",
        "255616956959",
    ),
)


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = replace(
            load_settings(),
            app_env="testing",
            host="127.0.0.1",
            port=0,
            database_path=str(Path(self.temp.name) / "gateway.sqlite3"),
            log_path=str(Path(self.temp.name) / "gateway.log"),
            dry_run_whatsapp=True,
            whatsapp_template_name="approval_request",
            meta_app_secret="meta-test-secret",
            whatsapp_phone_number_id="123",
        )
        init_db(self.settings)
        migrate(self.settings)
        init_workflow_events(self.settings)
        self.level = 1
        self.record = {
            "id": 9,
            "company_id": 77,
            "status": "PENDING",
            "is_closed": 0,
            "items": [{"requisition_item_id": 1, "total_amount": 250000}],
            "remarks": "Parts",
            "requested_by": "Workshop Manager",
            "sources": [],
        }
        self.peta = PetaClient("https://peta.example/api/v1")
        self.peta._get = Mock(side_effect=self.peta_response)
        directory = PetaDirectoryClient(
            "https://directory.example/api", "test-token", {}, {}
        )
        directory._get = Mock(side_effect=self.directory_response)
        configure_workflow_guards(
            {"peta": PetaWorkflowGuard(self.peta, directory, "255")}
        )
        self.addCleanup(lambda: configure_workflow_guards({}))
        with connection(self.settings) as c:
            create_api_client(c, "peta", "test-key")
            create_api_client(c, "other", "other-key")
            c.execute(
                "INSERT INTO integrations VALUES ('peta', 'http://127.0.0.1:1/callback', ?)",
                ("s" * 32,),
            )
            for index, (identity, uid, name, phone) in enumerate(ACTORS, 1):
                c.execute(
                    "INSERT INTO approvers VALUES (?, ?, 'assigned', '77', ?, 1, 'now', 'now')",
                    (index, name, phone),
                )
                c.execute(
                    "INSERT INTO actor_bindings(system_name,company,directory_uuid,source_user_id,approver_id) VALUES ('peta','77',?,?,?)",
                    (identity, str(uid), index),
                )

    def peta_response(self, path):
        if path == "requisitions/9/details":
            return {"success": True, "data": copy.deepcopy(self.record)}
        if path != "requisitions/9/approval-chain":
            raise AssertionError(f"Unexpected endpoint: {path}")
        levels = []
        for index, (_, uid, name, _) in enumerate(ACTORS, 1):
            levels.append(
                {
                    "id": index + 10,
                    "level_id": index,
                    "is_active": True,
                    "approval_status": (
                        "APPROVED" if index < self.level else "PENDING"
                    ),
                    "approvers": [
                        {
                            "id": uid,
                            "username": name,
                            "email": name + "@example.com",
                        }
                    ],
                    "role": {"name": "CHECK" if index == 1 else "VERIFY"},
                    "position": {
                        "role_name": "Accountant" if index == 1 else "Director"
                    },
                }
            )
        return {
            "success": True,
            "data": {
                "requisition_id": 9,
                "module": {"id": 3},
                "levels": levels,
            },
        }

    def directory_response(self, path):
        uid = int(path.split("/")[1])
        identity, _, name, phone = next(
            actor for actor in ACTORS if actor[1] == uid
        )
        return {
            "status": "success",
            "user_details": {
                "id": uid,
                "uuid": identity,
                "username": name,
                "email": name + "@example.com",
                "phone_number": phone,
                "phone_verified_at": None,
                "is_active": 1,
                "banned_at": None,
                "banned_until": None,
            },
        }

    def trigger(self, **fields):
        data = dict(
            event_id=str(uuid4()),
            source_system="peta",
            request_type="requisition_approval",
            reference_id="9",
        )
        data.update(fields)
        return accept_workflow_event(self.settings, "test-key", data)

    def task(self):
        with connection(self.settings) as c:
            return dict(
                c.execute(
                    "SELECT * FROM approval_requests ORDER BY id DESC LIMIT 1"
                ).fetchone()
            )

    def reply(self, task, **fields):
        value = dict(
            action="approved",
            reference=task["gateway_reference_id"],
            phone=task["actor_phone"],
            message_id=str(uuid4()),
            text="Approve",
            context_id=None,
        )
        value.update(fields)
        return decide(self.settings, value)

    def run_worker(self):
        with patch("approval_gateway.worker.WhatsAppClient") as client:
            client.return_value.send_text.return_value = "wamid.text"
            client.return_value.send_approval_request.return_value = (
                "wamid.mock"
            )
            result = run_once(self.settings)
        return result, client

    def test_event_to_delivery_and_next_step(self):
        self.trigger()
        result, client = self.run_worker()
        self.assertEqual(result["workflow_events"], 1)
        self.assertEqual(result["delivery_attempts"], 1)
        self.assertEqual(self.task()["step_id"], "11")
        self.assertEqual(
            client.return_value.send_approval_request.call_args.args[0],
            ACTORS[0][3],
        )
        self.level = 2
        self.record["status"] = "APPROVED"
        self.trigger()
        self.run_worker()
        self.assertEqual(self.task()["step_id"], "12")
        self.assertEqual(self.task()["actor_user_id"], "4")

    def test_duplicate_event_and_repeated_trigger_do_not_duplicate_tasks(self):
        event_id = str(uuid4())
        self.trigger(event_id=event_id)
        self.assertFalse(self.trigger(event_id=event_id)[1])
        self.run_worker()
        self.trigger()
        self.run_worker()
        with connection(self.settings) as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[
                    0
                ],
                1,
            )

    def test_event_source_is_authenticated(self):
        with self.assertRaises(ServiceError):
            self.trigger(source_system="other")

    def test_atomic_tasks_and_completion_when_binding_missing(self):
        with connection(self.settings) as c:
            c.execute("DELETE FROM actor_bindings")
        self.trigger()
        result, client = self.run_worker()
        client.return_value.send_approval_request.assert_not_called()
        with connection(self.settings) as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[
                    0
                ],
                0,
            )
            self.assertEqual(
                c.execute("SELECT status FROM workflow_events").fetchone()[0],
                "failed",
            )

    def test_unclaimed_event_cannot_create_tasks(self):
        self.trigger()
        with self.assertRaises(ServiceError):
            process_workflow_event(self.settings, 1)

    def test_interrupted_event_is_recovered(self):
        self.trigger()
        with connection(self.settings) as c:
            c.execute("UPDATE workflow_events SET status='processing'")
        self.run_worker()
        with connection(self.settings) as c:
            self.assertEqual(
                c.execute("SELECT status FROM workflow_events").fetchone()[0],
                "completed",
            )

    def test_unavailable_erp_is_retryable(self):
        self.trigger()
        self.peta._get.side_effect = IntegrationUnavailable("unavailable")
        self.run_worker()
        with connection(self.settings) as c:
            row = c.execute("SELECT * FROM workflow_events").fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], 1)

    def test_stale_task_is_not_delivered(self):
        self.trigger()
        with connection(self.settings) as c:
            c.execute(
                "UPDATE workflow_events SET status='processing', attempts=1"
            )
        process_workflow_event(self.settings, 1)
        self.level = 2
        _, client = self.run_worker()
        client.return_value.send_approval_request.assert_not_called()
        self.assertEqual(self.task()["delivery_status"], "blocked")

    def test_wrong_sender_and_changed_record_are_denied(self):
        self.trigger()
        self.run_worker()
        task = self.task()
        with self.assertRaises(ServiceError):
            self.reply(task, phone=ACTORS[1][3])
        self.record["remarks"] = "Changed"
        with self.assertRaises(ServiceError):
            self.reply(task)

    def test_opposite_decision_keeps_one_confirmation_and_callback(self):
        self.trigger()
        self.run_worker()
        task = self.task()
        self.reply(task, action="rejected", text="Reject")
        self.assertFalse(self.reply(task, action="approved")["processed"])
        with connection(self.settings) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM callback_attempts").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM decision_notifications").fetchone()[0], 1)
        self.assertEqual(self.task()["status"], "rejected")

    def test_confirmation_retry_does_not_change_decision(self):
        self.trigger()
        self.run_worker()
        self.reply(self.task())
        client = Mock()
        client.send_text.side_effect = WhatsAppError("rate limit", http_status=429)
        process_notifications(self.settings, client, 25)
        with connection(self.settings) as c:
            row = c.execute("SELECT * FROM decision_notifications").fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], 1)
            c.execute("UPDATE decision_notifications SET next_attempt_at='2000-01-01T00:00:00+00:00'")
        client.send_text.side_effect = None
        client.send_text.return_value = "wamid.confirmation"
        process_notifications(replace(self.settings, dry_run_whatsapp=False), client, 25)
        process_notifications(self.settings, client, 25)
        self.assertEqual(client.send_text.call_count, 2)
        self.assertEqual(self.task()["execution_status"], "pending")

    def test_unknown_confirmation_is_not_resent(self):
        self.trigger()
        self.run_worker()
        self.reply(self.task())
        client = Mock()
        client.send_text.side_effect = WhatsAppError("timeout", delivery_unknown=True)
        process_notifications(self.settings, client, 25)
        process_notifications(self.settings, client, 25)
        self.assertEqual(client.send_text.call_count, 1)
        with connection(self.settings) as c:
            self.assertEqual(c.execute("SELECT status FROM decision_notifications").fetchone()[0], "unknown")

    def test_expired_confirmation_and_interrupted_send_are_not_sent(self):
        self.trigger()
        self.run_worker()
        self.reply(self.task())
        client = Mock()
        with connection(self.settings) as c:
            c.execute("UPDATE decision_notifications SET expires_at='2000-01-01T00:00:00+00:00'")
        process_notifications(self.settings, client, 25)
        client.send_text.assert_not_called()
        with connection(self.settings) as c:
            self.assertEqual(c.execute("SELECT status FROM decision_notifications").fetchone()[0], "expired")
            c.execute("UPDATE decision_notifications SET status='sending'")
        process_notifications(self.settings, client, 25)
        client.send_text.assert_not_called()
        with connection(self.settings) as c:
            self.assertEqual(c.execute("SELECT status FROM decision_notifications").fetchone()[0], "unknown")

    def test_confirmation_result_is_deduplicated_and_preserves_step_scope(self):
        self.trigger()
        self.run_worker()
        self.reply(self.task())
        task = self.task()
        task["execution_status"] = "applied"
        with connection(self.settings) as c:
            enqueue_notification(c, task, "result", task["responded_at"])
            enqueue_notification(c, task, "result", task["responded_at"])
        client = Mock()
        client.send_text.return_value = "wamid.confirmation"
        process_notifications(self.settings, client, 25)
        self.assertEqual(client.send_text.call_count, 2)
        self.assertIn("kwa hatua uliyopewa", client.send_text.call_args.args[1])
        with connection(self.settings) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM decision_notifications").fetchone()[0], 2)

    def test_duplicate_decision_has_one_callback(self):
        self.trigger()
        self.run_worker()
        task = self.task()
        message_id = str(uuid4())
        self.assertTrue(self.reply(task, message_id=message_id)["processed"])
        self.assertFalse(self.reply(task, message_id=message_id)["processed"])
        with connection(self.settings) as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM callback_attempts").fetchone()[
                    0
                ],
                1,
            )

    def test_callback_signature_confirmation_and_retry(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                timestamp = self.headers["X-Approval-Gateway-Timestamp"]
                expected = hmac.new(
                    b"s" * 32, timestamp.encode() + b"." + body, hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(
                    expected, self.headers["X-Approval-Gateway-Signature"]
                ):
                    self.send_response(403)
                    self.end_headers()
                    return
                event = json.loads(body)
                received.append(event)
                if len(received) == 1:
                    self.send_response(503)
                    self.end_headers()
                    return
                reply = json.dumps(
                    {
                        "event_id": event["event_id"],
                        "execution_status": "applied",
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with connection(self.settings) as c:
                c.execute(
                    "UPDATE integrations SET callback_url=?",
                    (f"http://127.0.0.1:{server.server_port}/callback",),
                )
            self.trigger()
            self.run_worker()
            task = self.task()
            self.reply(task)
            self.run_worker()
            self.assertEqual(self.task()["execution_status"], "pending")
            with connection(self.settings) as c:
                c.execute(
                    "UPDATE callback_attempts SET next_attempt_at='2000-01-01'"
                )
            self.run_worker()
            self.assertEqual(self.task()["execution_status"], "applied")
            with connection(self.settings) as c:
                notices = c.execute("SELECT kind, status FROM decision_notifications ORDER BY id").fetchall()
                self.assertEqual([(r["kind"], r["status"]) for r in notices], [("received", "simulated"), ("result", "simulated")])
            self.assertEqual(received[0]["event_id"], received[1]["event_id"])
            self.assertIn("record_snapshot", received[0]["payload"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_http_event_endpoint_acknowledges_and_deduplicates(self):
        server = Server(self.settings)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        payload = json.dumps(
            dict(
                event_id=str(uuid4()),
                source_system="peta",
                request_type="requisition_approval",
                reference_id="9",
            )
        ).encode()
        try:
            for expected in (202, 200):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/workflow-events",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer test-key",
                    },
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, expected)
                    self.assertTrue(json.load(response)["accepted"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_worker_lock_prevents_concurrent_batches(self):
        with _worker_lock(self.settings) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(
                run_once(self.settings)["skipped"], "worker_already_running"
            )

    def test_direct_api_creation_uses_same_persistence(self):
        adapter = __import__(
            "approval_gateway.integrations.registry",
            fromlist=["get_workflow_guard"],
        ).get_workflow_guard("peta")
        candidate = adapter.resolve_workflow_event(
            dict(
                source_system="peta",
                request_type="requisition_approval",
                reference_id="9",
            )
        )[0]
        first, is_new = create(self.settings, "test-key", candidate)
        second, is_new_again = create(self.settings, "test-key", candidate)
        self.assertTrue(is_new)
        self.assertFalse(is_new_again)
        self.assertEqual(first["id"], second["id"])

    def test_document_template_uses_pdf_header_and_current_step(self):
        settings = replace(
            self.settings,
            dry_run_whatsapp=False,
            whatsapp_template_name="workflow_approval_document",
            whatsapp_access_token="fake",
            whatsapp_phone_number_id="123",
        )
        self.peta.get_document = Mock(return_value=b"%PDF-1.4\nmock")
        self.trigger()
        with patch(
            "approval_gateway.upload_document.upload_pdf", return_value="987"
        ) as upload, patch(
            "approval_gateway.whatsapp.WhatsAppClient._post_graph",
            return_value={"messages": [{"id": "wamid.document"}]},
        ) as post:
            run_once(settings)
        upload.assert_called_once()
        self.peta.get_document.assert_called_once_with(9)
        components = post.call_args.args[0]["template"]["components"]
        self.assertEqual(
            components[0]["parameters"][0]["document"]["id"], "987"
        )
        self.assertEqual(
            components[1]["parameters"][2]["text"], "CHECK — Accountant"
        )
        self.assertEqual(
            components[2]["parameters"][0]["payload"],
            "approve_" + self.task()["gateway_reference_id"],
        )
        self.assertEqual(self.task()["delivery_status"], "sent")

    def test_document_preparation_failure_is_retryable(self):
        settings = replace(
            self.settings,
            dry_run_whatsapp=False,
            whatsapp_template_name="workflow_approval_document",
        )
        self.peta.get_document = Mock(
            side_effect=IntegrationUnavailable("unavailable")
        )
        self.trigger()
        with patch(
            "approval_gateway.whatsapp.WhatsAppClient._post_graph"
        ) as post:
            run_once(settings)
        post.assert_not_called()
        self.assertEqual(self.task()["delivery_status"], "pending")

    def test_record_changes_during_pdf_upload_prevent_message(self):
        settings = replace(
            self.settings,
            dry_run_whatsapp=False,
            whatsapp_template_name="workflow_approval_document",
            whatsapp_access_token="fake",
            whatsapp_phone_number_id="123",
        )
        self.peta.get_document = Mock(return_value=b"%PDF-1.4\nmock")
        self.trigger()

        def uploaded(*args):
            self.record["remarks"] = "Changed during upload"
            return "987"

        with patch(
            "approval_gateway.upload_document.upload_pdf", side_effect=uploaded
        ), patch(
            "approval_gateway.whatsapp.WhatsAppClient._post_graph"
        ) as post:
            run_once(settings)
        post.assert_not_called()
        self.assertEqual(self.task()["delivery_status"], "blocked")

    def test_two_candidates_rollback_together_if_second_binding_is_missing(
        self,
    ):
        original = self.peta_response

        def response(path):
            value = original(path)
            if path.endswith("/approval-chain"):
                _, uid, name, _ = ACTORS[1]
                value["data"]["levels"][0]["approvers"].append(
                    {
                        "id": uid,
                        "username": name,
                        "email": name + "@example.com",
                    }
                )
            return value

        self.peta._get.side_effect = response
        with connection(self.settings) as c:
            c.execute("DELETE FROM actor_bindings WHERE approver_id=2")
        self.trigger()
        self.run_worker()
        with connection(self.settings) as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[
                    0
                ],
                0,
            )
            self.assertEqual(
                c.execute("SELECT status FROM workflow_events").fetchone()[0],
                "failed",
            )

    def test_signed_real_webhook_route_records_one_decision(self):
        self.trigger()
        self.run_worker()
        task = self.task()
        server = Server(self.settings)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Match the Meta webhook envelope required by the parser.
        payload = json.dumps(
            {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "metadata": {
                                        "phone_number_id": self.settings.whatsapp_phone_number_id,
                                    },
                                    "messages": [
                                        {
                                            "type": "button",
                                            "id": "wamid.inbound",
                                            "from": task["actor_phone"],
                                            "button": {
                                                "payload": (
                                                    "approve_"
                                                    + task[
                                                        "gateway_reference_id"
                                                    ]
                                                ),
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        ).encode("utf-8")
        signature = (
            "sha256="
            + hmac.new(
                self.settings.meta_app_secret.encode(), payload, hashlib.sha256
            ).hexdigest()
        )
        try:
            for processed in (True, False):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/webhook/whatsapp",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": signature,
                    },
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        json.load(response)["results"][0]["processed"],
                        processed,
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
