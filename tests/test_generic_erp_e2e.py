"""Local end-to-end generic ERP test: actual HTTP API, verification and signed callback.

No live Meta, PETA, network service or production credentials are needed.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from approval_gateway.callbacks import verify_callback
from approval_gateway.config import Settings
from approval_gateway.db import connection, init_db, utc_now
from approval_gateway.integrations.generic import GenericWorkflowGuard
from approval_gateway.integrations.registry import configure_workflow_guards
from approval_gateway.repository import create_api_client
from approval_gateway.server import Server
from approval_gateway.upgrade import migrate
from approval_gateway.worker import run_once

ACTOR = "550e8400-e29b-41d4-a716-446655440000"
PHONE = "+255712345678"
VERIFY_TOKEN = "v" * 40
CALLBACK_SECRET = "s" * 40


class GenericERPHTTPIntegrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.settings = Settings(
            app_env="testing", host="127.0.0.1", port=8088,
            database_path=str(root / "gateway.sqlite3"),
            log_path=str(root / "gateway.log"),
            public_base_url="http://127.0.0.1:8088",
            default_expiry_hours=24, callback_timeout_seconds=3,
            callback_secret="", meta_verify_token="test",
            meta_app_secret="test", whatsapp_access_token="test",
            whatsapp_phone_number_id="123", whatsapp_api_version="v23.0",
            whatsapp_template_name="workflow_approval_document",
            whatsapp_template_language="en", dry_run_whatsapp=True)
        self.authorized = True
        self.step = "manager"
        self.seen_callbacks = []
        self.callback_failures_remaining = 0
        parent = self

        class ERPHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(raw)
                if self.path == "/verify":
                    if self.headers.get("Authorization") != "Bearer " + VERIFY_TOKEN:
                        self.send_response(401)
                        self.end_headers()
                        return
                    if (not parent.authorized or payload.get("step_id") != parent.step
                            or payload.get("company") != "14"
                            or payload.get("actor_directory_uuid") != ACTOR):
                        self.send_response(403)
                        self.end_headers()
                        return
                    response = {
                        "authorized": True, "company": payload["company"],
                        "request_type": payload["request_type"],
                        "reference_id": payload["reference_id"],
                        "step_id": payload["step_id"],
                        "actor_directory_uuid": ACTOR,
                        "source_user_id": "152", "phone_number": PHONE,
                        "module_id": "leave", "record_digest": "record-v1"}
                elif self.path == "/callback":
                    if not verify_callback(
                            raw, self.headers.get("X-Approval-Gateway-Timestamp"),
                            self.headers.get("X-Approval-Gateway-Signature"),
                            CALLBACK_SECRET):
                        self.send_response(403)
                        self.end_headers()
                        return
                    parent.seen_callbacks.append(payload)
                    if parent.callback_failures_remaining:
                        parent.callback_failures_remaining -= 1
                        self.send_response(503)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    response = {"event_id": payload["event_id"],
                                "execution_status": "applied"}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.erp_server = ThreadingHTTPServer(("127.0.0.1", 0), ERPHandler)
        self.erp_thread = threading.Thread(
            target=self.erp_server.serve_forever, daemon=True)
        self.erp_thread.start()
        self.addCleanup(self._shutdown_erp)

        erp_base = f"http://127.0.0.1:{self.erp_server.server_port}"
        guard = GenericWorkflowGuard(erp_base + "/verify", VERIFY_TOKEN,
                                     allow_local_http=True)
        configure_workflow_guards({"ghrm": guard})
        self.addCleanup(configure_workflow_guards, {})
        init_db(self.settings)
        migrate(self.settings)
        with connection(self.settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            create_api_client(conn, "ghrm", "local-ghrm-key")
            conn.execute(
                "INSERT INTO integrations(system_name,callback_url,callback_secret)"
                " VALUES (?,?,?)", ("ghrm", erp_base + "/callback", CALLBACK_SECRET))
        self.gateway = Server(replace(self.settings, port=0))
        self.gateway_thread = threading.Thread(
            target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()
        self.addCleanup(self._shutdown_gateway)
        self.base = f"http://127.0.0.1:{self.gateway.server_port}"
        self.body = {
            "source_system": "ghrm", "company": "14",
            "request_type": "leave_approval", "reference_id": "LEAVE-001",
            "step_id": "manager", "workflow_version": "record-v1",
            "actor_directory_uuid": ACTOR,
            "message": "Leave request requires approval",
            "idempotency_key": "ghrm-LEAVE-001-manager-record-v1",
            "payload": {"employee_id": 123, "leave_days": 5}}

    def _shutdown_gateway(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(timeout=5)

    def _shutdown_erp(self):
        self.erp_server.shutdown()
        self.erp_server.server_close()
        self.erp_thread.join(timeout=5)

    def post(self, data, key="local-ghrm-key"):
        request = urllib.request.Request(
            self.base + "/api/approval-requests",
            json.dumps(data).encode(), method="POST",
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def test_api_to_verified_decision_to_signed_erp_callback(self):
        code, created = self.post(self.body)
        self.assertEqual(code, 201)
        self.assertTrue(created["created"])
        ref = created["approval_request"]["gateway_reference_id"]
        code, repeated = self.post(self.body)
        self.assertEqual(code, 200)
        self.assertFalse(repeated["created"])
        self.assertEqual(ref, repeated["approval_request"]["gateway_reference_id"])
        code, _ = self.post(self.body, key="wrong-key")
        self.assertEqual(code, 401)
        self.authorized = False
        code, _ = self.post({**self.body, "reference_id": "LEAVE-002",
                             "idempotency_key": "leave-002"})
        self.assertEqual(code, 403)
        self.authorized = True

        # Simulate Meta's verified interactive decision using the gateway service.
        from approval_gateway.service import decide
        result = decide(self.settings, {
            "action": "approved", "reference": ref, "context_id": None,
            "phone": PHONE, "message_id": "wa-inbound-1", "text": "Approve"})
        self.assertTrue(result["processed"])
        # A local dry run queues delivery but never calls the live Meta API.
        for _ in range(4):
            run_once(self.settings)
        with connection(self.settings) as conn:
            row = conn.execute(
                "SELECT execution_status FROM approval_requests "
                "WHERE gateway_reference_id=?", (ref,)).fetchone()
        self.assertEqual(row["execution_status"], "applied")
        self.assertEqual(len(self.seen_callbacks), 1)
        self.assertEqual(self.seen_callbacks[0]["gateway_reference_id"], ref)

    def _decision(self, reference, message_id="wa-inbound-1"):
        from approval_gateway.service import decide
        return decide(self.settings, {
            "action": "approved", "reference": reference, "context_id": None,
            "phone": PHONE, "message_id": message_id, "text": "Approve"})

    def _execution(self, reference):
        with connection(self.settings) as conn:
            return dict(conn.execute(
                "SELECT status, execution_status FROM approval_requests "
                "WHERE gateway_reference_id=?", (reference,)).fetchone())

    def test_revoked_approver_cannot_decide(self):
        code, created = self.post(self.body)
        self.assertEqual(code, 201)
        ref = created["approval_request"]["gateway_reference_id"]
        self.authorized = False
        from approval_gateway.service import ServiceError
        with self.assertRaises(ServiceError) as caught:
            self._decision(ref)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self._execution(ref)["status"], "pending")
        with connection(self.settings) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM callback_attempts").fetchone()[0], 0)
        self.assertEqual(self.seen_callbacks, [])

    def test_duplicate_interactive_decision_is_not_applied_twice(self):
        code, created = self.post(self.body)
        self.assertEqual(code, 201)
        ref = created["approval_request"]["gateway_reference_id"]
        first = self._decision(ref)
        self.assertTrue(first["processed"])
        duplicated = self._decision(ref)
        self.assertEqual(duplicated["reason"], "duplicate_message")
        run_once(self.settings)
        self.assertEqual(self._execution(ref)["execution_status"], "applied")
        self.assertEqual(len(self.seen_callbacks), 1)
        with connection(self.settings) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM callback_attempts").fetchone()[0], 1)

    def test_callback_503_retries_with_same_event_id(self):
        code, created = self.post(self.body)
        self.assertEqual(code, 201)
        ref = created["approval_request"]["gateway_reference_id"]
        self.assertTrue(self._decision(ref)["processed"])
        self.callback_failures_remaining = 1
        run_once(self.settings)
        self.assertEqual(self._execution(ref)["execution_status"], "pending")
        self.assertEqual(len(self.seen_callbacks), 1)
        with connection(self.settings) as conn:
            row = conn.execute("SELECT status, attempts FROM callback_attempts").fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], 1)
            # Advance only this isolated test database's retry clock.
            conn.execute("UPDATE callback_attempts SET next_attempt_at=?", (utc_now(),))
        run_once(self.settings)
        self.assertEqual(self._execution(ref)["execution_status"], "applied")
        self.assertEqual(len(self.seen_callbacks), 2)
        self.assertEqual(
            self.seen_callbacks[0]["event_id"], self.seen_callbacks[1]["event_id"])
        with connection(self.settings) as conn:
            row = conn.execute("SELECT status, attempts FROM callback_attempts").fetchone()
            self.assertEqual(row["status"], "sent")
            self.assertEqual(row["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
