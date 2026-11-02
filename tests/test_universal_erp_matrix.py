"""Universal ERP matrix: one unchanged Gateway contract, three independently scoped ERP backends.

All servers and SQLite databases are ephemeral; no Meta, PETA or production access.
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
from approval_gateway.db import connection, init_db
from approval_gateway.integrations.generic import GenericWorkflowGuard
from approval_gateway.integrations.registry import configure_workflow_guards
from approval_gateway.repository import create_api_client
from approval_gateway.server import Server
from approval_gateway.service import decide
from approval_gateway.upgrade import migrate
from approval_gateway.worker import run_once


# Only values vary; the JSON request field names and Gateway endpoints do not.
SYSTEMS = (
    ("ghrm", "14", "leave_approval", "LEAVE-001", "manager", "leave", {"employee_id": 123, "days": 5}),
    ("abs", "20", "payment_approval", "PAY-001", "finance", "payment", {"invoice_id": 456, "amount": 90000}),
    ("macargo", "30", "shipment_approval", "SHIP-001", "supervisor", "shipment", {"cargo_id": 789, "route": "DSM-MOR"}),
)
CONTRACT_KEYS = frozenset({
    "source_system", "company", "request_type", "reference_id", "step_id",
    "workflow_version", "actor_directory_uuid", "message", "idempotency_key", "payload",
})


class UniversalERPMatrixTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.settings = Settings(
            app_env="testing", host="127.0.0.1", port=0,
            database_path=str(root / "matrix.sqlite3"),
            log_path=str(root / "matrix.log"),
            public_base_url="http://127.0.0.1:8088",
            default_expiry_hours=24, callback_timeout_seconds=3,
            callback_secret="", meta_verify_token="test", meta_app_secret="test",
            whatsapp_access_token="test", whatsapp_phone_number_id="123",
            whatsapp_api_version="v23.0", whatsapp_template_name="workflow_approval_document",
            whatsapp_template_language="en", dry_run_whatsapp=True,
        )
        self.backends = {}
        guards = {}
        init_db(self.settings)
        migrate(self.settings)
        for idx, (system, company, req_type, reference, step, module, metadata) in enumerate(SYSTEMS, start=1):
            actor = f"550e8400-e29b-41d4-a716-44665544000{idx}"
            phone = f"+25571234000{idx}"
            token = system + ("v" * 40)
            secret = system + ("s" * 40)
            expected = {
                "company": company, "request_type": req_type, "reference_id": reference,
                "step_id": step, "actor_directory_uuid": actor, "workflow_version": "record-v1",
            }
            backend = {"system": system, "actor": actor, "phone": phone,
                       "token": token, "secret": secret, "expected": expected,
                       "module": module, "callbacks": [], "authorized": True}
            self.backends[system] = backend

            # Build a closure so the server's handler has its own backend and user ID.
            def make_handler(b, user_id):
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_POST(self):
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 65536:
                            self.send_error(400)
                            return
                        raw = self.rfile.read(length)
                        try:
                            obj = json.loads(raw)
                        except (ValueError, UnicodeError):
                            self.send_error(400)
                            return
                        if self.path == "/verify":
                            if self.headers.get("Authorization") != "Bearer " + b["token"]:
                                self.send_error(401)
                                return
                            valid = obj == b["expected"] or obj == {
                                k: v for k, v in b["expected"].items() if k != "workflow_version"
                            }
                            if not valid or not b["authorized"]:
                                self.send_error(403)
                                return
                            result = {
                                "authorized": True,
                                **{k: b["expected"][k] for k in (
                                    "company", "request_type", "reference_id",
                                    "step_id", "actor_directory_uuid")},
                                "source_user_id": str(user_id),
                                "phone_number": b["phone"], "module_id": b["module"],
                                "record_digest": "record-v1",
                            }
                        elif self.path == "/callback":
                            if not verify_callback(
                                raw, self.headers.get("X-Approval-Gateway-Timestamp"),
                                self.headers.get("X-Approval-Gateway-Signature"), b["secret"]
                            ) or obj.get("source_system") != b["system"]:
                                self.send_error(403)
                                return
                            b["callbacks"].append(obj)
                            result = {"event_id": obj["event_id"], "execution_status": "applied"}
                        else:
                            self.send_error(404)
                            return
                        raw_result = json.dumps(result).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(raw_result)))
                        self.end_headers()
                        self.wfile.write(raw_result)
                return Handler

            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(backend, 100 + idx))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda s=server, t=thread: (s.shutdown(), s.server_close(), t.join(timeout=5)))
            url = f"http://127.0.0.1:{server.server_port}"
            backend["url"] = url
            guards[system] = GenericWorkflowGuard(url + "/verify", token, allow_local_http=True)
            with connection(self.settings) as conn:
                conn.execute("BEGIN IMMEDIATE")
                create_api_client(conn, system, system + "-matrix-key")
                conn.execute(
                    "INSERT INTO integrations(system_name,callback_url,callback_secret) VALUES (?,?,?)",
                    (system, url + "/callback", secret),
                )
        configure_workflow_guards(guards)
        self.addCleanup(configure_workflow_guards, {})
        self.gateway = Server(replace(self.settings, port=0))
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()
        self.addCleanup(self.close_gateway)
        self.url = f"http://127.0.0.1:{self.gateway.server_port}"

    def close_gateway(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(timeout=5)

    def request(self, method, path, system, body=None):
        raw = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.url + path, data=raw, method=method,
            headers={"Authorization": "Bearer " + system + "-matrix-key",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def payload(self, system, company, req_type, reference, step, metadata):
        backend = self.backends[system]
        return {
            "source_system": system, "company": company, "request_type": req_type,
            "reference_id": reference, "step_id": step,
            "workflow_version": "record-v1", "actor_directory_uuid": backend["actor"],
            "message": f"Approval required for {reference}",
            "idempotency_key": f"{system}-{reference}-{step}-record-v1",
            "payload": metadata,
        }

    def test_same_api_contract_across_three_erps_and_signed_callbacks(self):
        refs = {}
        for system, company, req_type, reference, step, module, metadata in SYSTEMS:
            with self.subTest(system=system):
                data = self.payload(system, company, req_type, reference, step, metadata)
                self.assertEqual(set(data), CONTRACT_KEYS)
                status, answer = self.request("POST", "/api/approval-requests", system, data)
                self.assertEqual(status, 201, answer)
                self.assertTrue(answer["created"])
                ref = answer["approval_request"]["gateway_reference_id"]
                refs[system] = ref
                status, again = self.request("POST", "/api/approval-requests", system, data)
                self.assertEqual(status, 200, again)
                self.assertFalse(again["created"])
                self.assertEqual(again["approval_request"]["gateway_reference_id"], ref)
                status, detail = self.request("GET", "/api/approval-requests/" + ref, system)
                self.assertEqual(status, 200, detail)
                self.assertEqual(detail["approval_request"]["source_system"], system)
                other_system = next(x[0] for x in SYSTEMS if x[0] != system)
                denied_status, _ = self.request("GET", "/api/approval-requests/" + ref, other_system)
                self.assertIn(denied_status, (403, 404))
                decision = decide(self.settings, {
                    "action": "approved", "reference": ref, "context_id": None,
                    "phone": self.backends[system]["phone"],
                    "message_id": "wa-" + system + "-1", "text": "Approve",
                })
                self.assertTrue(decision["processed"], decision)
        for _ in range(4):
            run_once(self.settings)
        with connection(self.settings) as conn:
            for system, *_ in SYSTEMS:
                row = conn.execute(
                    "SELECT status, execution_status FROM approval_requests WHERE gateway_reference_id=?",
                    (refs[system],),
                ).fetchone()
                self.assertEqual(row["status"], "approved")
                self.assertEqual(row["execution_status"], "applied")
                callbacks = self.backends[system]["callbacks"]
                self.assertEqual(len(callbacks), 1)
                self.assertEqual(callbacks[0]["gateway_reference_id"], refs[system])
                self.assertEqual(callbacks[0]["source_system"], system)

    def test_cross_erp_key_scope_and_revocation(self):
        first, second = SYSTEMS[:2]
        own = self.payload(*first[:5], first[6])
        other = self.payload(*second[:5], second[6])
        status, _ = self.request("POST", "/api/approval-requests", first[0], other)
        self.assertEqual(status, 403)
        status, result = self.request("POST", "/api/approval-requests", first[0], own)
        self.assertEqual(status, 201, result)
        self.backends[first[0]]["authorized"] = False
        status, _ = self.request("POST", "/api/approval-requests", first[0], {
            **own, "reference_id": "ANOTHER-REF",
        })
        self.assertEqual(status, 403)
        from approval_gateway.service import ServiceError
        with self.assertRaises(ServiceError) as caught:
            decide(self.settings, {
                "action": "approved",
                "reference": result["approval_request"]["gateway_reference_id"],
                "context_id": None,
                "phone": self.backends[first[0]]["phone"],
                "message_id": "wa-revoked", "text": "Approve",
            })
        self.assertEqual(caught.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
