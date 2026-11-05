"""Contract flow with an entirely external, mocked ERP HTTP interface."""
from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from approval_gateway.config import load_settings
from approval_gateway.db import connection, init_db
from approval_gateway.integrations.generic import GenericWorkflowGuard
from approval_gateway.integrations.registry import configure_workflow_guards
from approval_gateway.repository import create_api_client
from approval_gateway.service import create, decide, verify_delivery_workflow, ServiceError
from approval_gateway.upgrade import migrate
from approval_gateway.worker import _claim_callback, _send_callback

ACTOR = "0656c52c-970d-11f1-b365-6805cae19a58"


class HttpReply:
    status = 200
    def __init__(self, body):
        self.body = json.dumps(body).encode()
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def read(self, amount):
        return self.body[:amount]


class UniversalContractFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = replace(
            load_settings(), app_env="testing", dry_run_whatsapp=True,
            database_path=str(Path(temp.name) / "gateway.sqlite3"),
            log_path=str(Path(temp.name) / "gateway.log"),
        )
        init_db(self.settings)
        migrate(self.settings)
        self.step = "manager"
        self.digest = "revision-1"
        self.callback_calls = 0
        self.verified_versions = []
        self.guard = GenericWorkflowGuard("https://external.example.com/verify", "v" * 32)
        configure_workflow_guards({"external": self.guard})
        self.addCleanup(lambda: configure_workflow_guards({}))
        with connection(self.settings) as conn:
            create_api_client(conn, "external", "gateway-key")
            conn.execute("INSERT INTO integration_company_scopes (system_name, company) VALUES (?, ?)", ("external", "14"))
            conn.execute("INSERT INTO integrations (system_name,callback_url,callback_secret) VALUES (?,?,?)",
                         ("external", "https://external.example.com/callback", "s" * 32))
        from approval_gateway.identity_link import start, receive
        challenge = start(self.settings, "gateway-key", {
            "company": "14", "actor_directory_uuid": ACTOR,
            "source_user_id": "7", "phone_number": "255700000001",
        })
        linked = receive(self.settings, {
            "action": "conversation", "text": challenge["whatsapp_message"],
            "phone": "255700000001", "message_id": "wamid.fixture.link",
        })
        self.assertTrue(linked["processed"])

    def fake_open(self, request, **kwargs):
        url = request.full_url
        if url.endswith("/verify"):
            data = json.loads(request.data)
            self.verified_versions.append(data.get("workflow_version"))
            if data.get("step_id") != self.step:
                return HttpReply({"authorized": False})
            return HttpReply({"authorized": True, "actor": {
                "directory_uuid": ACTOR, "source_user_id": "7",
                "phone_number": "255700000001", "module_id": "leave",
                "step_id": self.step, "record_digest": self.digest,
            }})
        if url.endswith("/callback"):
            timestamp = request.get_header("X-approval-gateway-timestamp")
            signature = request.get_header("X-approval-gateway-signature")
            self.assertIsNotNone(timestamp)
            expected = hmac.new(("s" * 32).encode(),
                                timestamp.encode() + b"." + request.data,
                                hashlib.sha256).hexdigest()
            self.assertTrue(hmac.compare_digest(expected, signature))
            self.callback_calls += 1
            event = json.loads(request.data)
            return HttpReply({"event_id": event["event_id"],
                              "execution_status": "applied"})
        raise AssertionError("Unexpected network endpoint")

    def create_task(self):
        return create(self.settings, "gateway-key", {
            "source_system": "external", "company": "14",
            "request_type": "leave_approval", "reference_id": "LEAVE-1",
            "step_id": "manager", "workflow_version": "revision-1",
            "actor_directory_uuid": ACTOR, "message": "Please review",
            "payload": {"employee_id": "7"},
        })

    def test_unscoped_company_is_denied_before_erp_network_request(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("should not call ERP")):
            with self.assertRaises(ServiceError) as denied:
                create(self.settings, "gateway-key", {
                    "source_system": "external", "company": "99",
                    "request_type": "leave_approval", "reference_id": "LEAVE-99",
                    "step_id": "manager", "workflow_version": "revision-1",
                    "actor_directory_uuid": ACTOR, "message": "Please review",
                    "payload": {"employee_id": "7"},
                })
            self.assertEqual(denied.exception.status, 403)

    def test_scope_revocation_blocks_read_and_cancel(self):
        from approval_gateway.service import read_request, cancel
        with patch("urllib.request.OpenerDirector.open", side_effect=self.fake_open):
            task, _ = self.create_task()
        with connection(self.settings) as conn:
            conn.execute("DELETE FROM integration_company_scopes WHERE system_name=?", ("external",))
        for operation in (
            lambda: read_request(self.settings, "gateway-key", task["gateway_reference_id"]),
            lambda: cancel(self.settings, "gateway-key", task["gateway_reference_id"]),
        ):
            with self.assertRaises(ServiceError) as denied:
                operation()
            self.assertEqual(denied.exception.status, 403)

    def test_intake_rejects_erp_digest_version_mismatch(self):
        self.digest = "revision-2"
        with patch("urllib.request.OpenerDirector.open", side_effect=self.fake_open):
            with self.assertRaises(ServiceError) as denied:
                self.create_task()
            self.assertEqual(denied.exception.status, 409)
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM approval_requests"
            ).fetchone()[0], 0)

    def test_unverified_actor_cannot_enroll_from_erp_assertion(self):
        with connection(self.settings) as conn:
            conn.execute("DELETE FROM actor_bindings WHERE system_name=?", ("external",))
        with patch("urllib.request.OpenerDirector.open", side_effect=self.fake_open):
            with self.assertRaises(ServiceError) as denied:
                self.create_task()
            self.assertEqual(denied.exception.status, 403)
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM actor_bindings WHERE system_name=?",
                ("external",),
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM approval_requests"
            ).fetchone()[0], 0)

    def test_erp_phone_change_cannot_override_enrolled_phone(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=self.fake_open):
            with connection(self.settings) as conn:
                conn.execute("UPDATE approvers SET phone_number=? WHERE id=1", ("255700000099",))
            with self.assertRaises(ServiceError) as denied:
                self.create_task()
            self.assertEqual(denied.exception.status, 403)
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute(
                "SELECT phone_number FROM approvers WHERE id=1"
            ).fetchone()[0], "255700000099")

    def test_pre_delivery_verifies_record_version_and_payload(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=self.fake_open):
            task, _ = self.create_task()
            verify_delivery_workflow(self.settings, task)
            self.assertEqual(self.verified_versions, ["revision-1", "revision-1"])
            self.assertEqual(self.digest, "revision-1")

    def test_changed_record_digest_blocks_decision(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=self.fake_open):
            task, created = self.create_task()
            self.assertTrue(created)
            self.digest = "revision-2"
            with self.assertRaises(ServiceError) as error:
                decide(self.settings, {
                    "action": "approved", "reference": task["gateway_reference_id"],
                    "phone": "255700000001", "message_id": "stale-record",
                    "text": "Approve", "context_id": None,
                })
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(self.verified_versions, ["revision-1", "revision-1"])
        with connection(self.settings) as conn:
            task_state = conn.execute(
                "SELECT status, execution_status FROM approval_requests WHERE id=?",
                (task["id"],),
            ).fetchone()
            self.assertEqual(task_state["status"], "pending")
            self.assertEqual(task_state["execution_status"], "not_requested")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM callback_attempts").fetchone()[0], 0)

    def test_whatsapp_phone_link_to_generic_erp_execution(self):
        from approval_gateway.identity_link import start, receive, status
        with connection(self.settings) as conn:
            conn.execute("DELETE FROM actor_bindings WHERE system_name='external'")
        challenge = start(self.settings, "gateway-key", {
            "company": "14", "actor_directory_uuid": ACTOR,
            "source_user_id": "7", "phone_number": "255700000001",
        })
        self.assertFalse(status(self.settings, "gateway-key", ACTOR, "14")["verified"])
        linked = receive(self.settings, {
            "action": "conversation", "text": challenge["whatsapp_message"],
            "phone": "255700000001", "message_id": "wamid.link.contract",
        })
        self.assertTrue(linked["processed"])
        self.assertTrue(status(self.settings, "gateway-key", ACTOR, "14")["verified"])
        with patch("urllib.request.OpenerDirector.open", side_effect=self.fake_open):
            task, created = self.create_task()
            self.assertTrue(created)
            decided = decide(self.settings, {
                "action": "approved", "reference": task["gateway_reference_id"],
                "phone": "255700000001", "message_id": "wamid.decision.contract",
                "text": "Approve", "context_id": None,
            })
            self.assertTrue(decided["processed"])
            claimed = _claim_callback(self.settings)
            self.assertIsNotNone(claimed)
            _send_callback(self.settings, claimed)
        self.assertEqual(self.callback_calls, 1)
        with connection(self.settings) as conn:
            row = conn.execute("SELECT execution_status FROM approval_requests WHERE id=?",
                               (task["id"],)).fetchone()
            self.assertEqual(row["execution_status"], "applied")

    def test_rejects_malformed_approval_policy(self):
        for malformed in ({"rejection_reason_required": "true"},
                          {"rejection_reason_required": 1},
                          {"unknown_option": True}, None):
            with self.subTest(value=malformed):
                with patch("urllib.request.OpenerDirector.open", side_effect=self.fake_open):
                    with self.assertRaises(ServiceError) as denied:
                        create(self.settings, "gateway-key", {
                            "source_system": "external", "company": "14",
                            "request_type": "expense_approval",
                            "reference_id": "EX-100",
                            "step_id": "manager", "workflow_version": "revision-1",
                            "actor_directory_uuid": ACTOR, "message": "Expense",
                            "payload": {"approval_options": malformed},
                        })
                    self.assertEqual(denied.exception.status, 400)
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM approval_requests"
            ).fetchone()[0], 0)

    def test_generic_erp_can_require_rejection_reason(self):
        """Configured ERP types enter the existing verified reason-review flow."""
        from approval_gateway.rejection import requires_reason
        with patch("urllib.request.OpenerDirector.open", side_effect=self.fake_open):
            data = {
                "source_system": "external", "company": "14",
                "request_type": "expense_approval",
                "reference_id": "EXPENSE-9",
                "step_id": "manager", "workflow_version": "revision-1",
                "actor_directory_uuid": ACTOR,
                "message": "Expense requires review",
                "payload": {
                    "employee_id": "7",
                    "approval_options": {"rejection_reason_required": True},
                },
            }
            task, created = create(self.settings, "gateway-key", data)
            self.assertTrue(created)
            self.assertTrue(requires_reason(task))
            started = decide(self.settings, {
                "action": "rejected", "reference": task["gateway_reference_id"],
                "phone": "255700000001", "message_id": "wamid.generic.reason.1",
                "text": "Reject", "context_id": None,
            })
            self.assertTrue(started["processed"])
            self.assertEqual(started["reason"], "rejection_conversation")
            with connection(self.settings) as conn:
                state = conn.execute(
                    "SELECT status,rejection_phase,execution_status FROM approval_requests WHERE id=?",
                    (task["id"],),
                ).fetchone()
                self.assertEqual(state["status"], "pending")
                self.assertEqual(state["rejection_phase"], "choice")
                self.assertEqual(state["execution_status"], "not_requested")
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM callback_attempts WHERE approval_request_id=?",
                    (task["id"],)
                ).fetchone()[0], 0)
        self.assertEqual(self.callback_calls, 0)

    def test_generic_approval_reason_policy_is_opt_in(self):
        from approval_gateway.rejection import requires_reason
        cases = (
            ("external", "expense_approval", {}, False),
            ("external", "expense_approval", {"approval_options": {"rejection_reason_required": False}}, False),
            ("external", "expense_approval", {"approval_options": {"rejection_reason_required": True}}, True),
            ("peta", "requisition_approval", {}, True),
            ("peta", "payment_advice_approval", {}, True),
        )
        for system, kind, payload, expected in cases:
            with self.subTest(system=system, request_type=kind):
                self.assertEqual(requires_reason({
                    "source_system": system, "request_type": kind,
                    "payload_json": json.dumps(payload),
                }), expected)

    def test_multiple_erp_approval_types_and_decisions_share_one_contract(self):
        """Leave, purchasing and payment approvals use one generic adapter.

        The source ERP remains responsible for the meaning of each action.
        """
        cases = (
            ("leave_approval", "LEAVE-101", "approved"),
            ("purchase_order_approval", "PO-102", "rejected"),
            ("payment_approval", "PAY-103", "approved"),
        )
        with patch("urllib.request.OpenerDirector.open", side_effect=self.fake_open):
            for index, (request_type, reference, action) in enumerate(cases):
                with self.subTest(request_type=request_type):
                    data = {
                        "source_system": "external", "company": "14",
                        "request_type": request_type,
                        "reference_id": reference,
                        "step_id": "manager", "workflow_version": "revision-1",
                        "actor_directory_uuid": ACTOR,
                        "message": "Review " + request_type,
                        "payload": {"kind": request_type, "item": reference},
                        "idempotency_key": "external-" + reference + "-manager-v1",
                    }
                    task, created = create(self.settings, "gateway-key", data)
                    self.assertTrue(created)
                    same, created_again = create(self.settings, "gateway-key", data)
                    self.assertFalse(created_again)
                    self.assertEqual(same["id"], task["id"])
                    result = decide(self.settings, {
                        "action": action,
                        "reference": task["gateway_reference_id"],
                        "phone": "255700000001",
                        "message_id": "wamid.generic.type." + str(index),
                        "text": "Approve" if action == "approved" else "Reject",
                        "context_id": None,
                    })
                    self.assertTrue(result["processed"])
                    claimed = _claim_callback(self.settings)
                    self.assertIsNotNone(claimed)
                    _send_callback(self.settings, claimed)
                    with connection(self.settings) as conn:
                        saved = conn.execute(
                            "SELECT status, execution_status FROM approval_requests WHERE id=?",
                            (task["id"],)
                        ).fetchone()
                        self.assertEqual(saved["status"], action)
                        self.assertEqual(saved["execution_status"], "applied")
            self.assertEqual(self.callback_calls, len(cases))

    def test_create_decide_signed_callback_and_confirmation(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=self.fake_open):
            task, created = self.create_task()
            self.assertTrue(created)
            duplicate, was_created = self.create_task()
            self.assertFalse(was_created)
            self.assertEqual(duplicate["id"], task["id"])
            with self.assertRaises(ServiceError):
                decide(self.settings, {
                    "action": "approved", "reference": task["gateway_reference_id"],
                    "phone": "255700000002", "message_id": "wrong-phone",
                    "text": "Approve", "context_id": None,
                })
            result = decide(self.settings, {
                "action": "approved", "reference": task["gateway_reference_id"],
                "phone": "255700000001", "message_id": "good-phone",
                "text": "Approve", "context_id": None,
            })
            self.assertTrue(result["processed"])
            queued = _claim_callback(self.settings)
            self.assertIsNotNone(queued)
            _send_callback(self.settings, queued)
        self.assertEqual(self.callback_calls, 1)
        with connection(self.settings) as conn:
            row = conn.execute("SELECT execution_status FROM approval_requests WHERE id=?",
                               (task["id"],)).fetchone()
            self.assertEqual(row["execution_status"], "applied")


if __name__ == "__main__":
    unittest.main()
