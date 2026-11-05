"""Generic integration contract tests (no access to an ERP codebase)."""
import io
import json
import unittest
from unittest.mock import patch
from uuid import uuid4
from approval_gateway.integrations.generic import GenericWorkflowGuard, build_adapter
from approval_gateway.integrations.permissions import IntegrationUnavailable, WorkflowDenied

ACTOR = "0656c52c-970d-11f1-b365-6805cae19a58"


class Reply:
    status = 200
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, limit):
        return json.dumps(self.value).encode()[:limit]


class GenericAdapterTests(unittest.TestCase):
    def setUp(self):
        self.guard = GenericWorkflowGuard("https://erp.example.com/approval/verify", "a" * 32)
        self.payload = {
            "company": "14", "reference_id": "LEAVE-2026-001",
            "request_type": "leave_approval", "step_id": "manager",
            "actor_directory_uuid": ACTOR, "workflow_version": "v1",
            "payload": {"employee_id": 7},
        }
        self.actor = {
            "directory_uuid": ACTOR, "source_user_id": "7",
            "phone_number": "255700000001", "module_id": "leave",
            "step_id": "manager", "record_digest": "v1",
        }

    def test_accepts_verified_actor(self):
        with patch("urllib.request.OpenerDirector.open",
                   return_value=Reply({"authorized": True, "actor": self.actor})):
            result = self.guard.authorize(self.payload)
        self.assertEqual(result.source_user_id, "7")

    def test_rejects_wrong_actor(self):
        actor = dict(self.actor, directory_uuid=str(uuid4()))
        with patch("urllib.request.OpenerDirector.open",
                   return_value=Reply({"authorized": True, "actor": actor})):
            with self.assertRaises(WorkflowDenied):
                self.guard.authorize(self.payload)

    def test_fails_closed_on_erp_denial(self):
        with patch("urllib.request.OpenerDirector.open",
                   return_value=Reply({"authorized": False})):
            with self.assertRaises(WorkflowDenied):
                self.guard.authorize(self.payload)

    def test_fails_closed_on_missing_actor(self):
        with patch("urllib.request.OpenerDirector.open",
                   return_value=Reply({"authorized": True})):
            with self.assertRaises(IntegrationUnavailable):
                self.guard.authorize(self.payload)

    def test_rejects_insecure_url(self):
        with self.assertRaises(ValueError):
            GenericWorkflowGuard("http://erp.example.com/verify", "a" * 32)

    def test_build_from_environment_reference(self):
        guard = build_adapter(
            {"verify_url": "https://erp.example.com/verify",
             "verify_token_env": "GENERIC_ERP_VERIFY_TOKEN"},
            {"GENERIC_ERP_VERIFY_TOKEN": "b" * 32},
        )
        self.assertEqual(guard.token, "b" * 32)


if __name__ == "__main__":
    unittest.main()
