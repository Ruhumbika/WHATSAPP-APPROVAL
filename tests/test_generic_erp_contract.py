from __future__ import annotations
import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import Mock

from approval_gateway.integrations.generic import (
    GenericWorkflowGuard, build_adapter,
)
from approval_gateway.integrations.permissions import (
    WorkflowDenied, IntegrationUnavailable,
)

UUID = "550e8400-e29b-41d4-a716-446655440000"
TOKEN = "v" * 40
REQUEST = {
    "company": "14",
    "request_type": "leave_approval",
    "reference_id": "LEAVE-001",
    "step_id": "manager",
    "actor_directory_uuid": UUID,
    "workflow_version": "record-v1",
}
VERIFIED = {
    **{key: REQUEST[key] for key in (
        "company", "request_type", "reference_id", "step_id",
        "actor_directory_uuid")},
    "authorized": True,
    "source_user_id": "152",
    "phone_number": "+255712345678",
    "module_id": "leave",
    "record_digest": "record-v1",
}


def response(data):
    msg = Message()
    msg["Content-Type"] = "application/json"
    obj = Mock()
    obj.status = 200
    obj.headers = msg
    obj.read.side_effect = lambda n: json.dumps(data).encode()
    obj.__enter__ = Mock(return_value=obj)
    obj.__exit__ = Mock(return_value=False)
    return obj


class GenericContractTests(unittest.TestCase):
    def setUp(self):
        self.guard = GenericWorkflowGuard(
            "https://erp.example/api/approvals/verify", TOKEN)

    def authorize(self, data=VERIFIED, request=REQUEST):
        self.guard.opener = Mock()
        self.guard.opener.open.return_value = response(data)
        return self.guard.authorize(request)

    def test_valid_current_actor(self):
        actor = self.authorize()
        self.assertEqual(actor.directory_uuid, UUID)
        self.assertEqual(actor.phone_number, "+255712345678")
        sent = self.guard.opener.open.call_args.args[0]
        self.assertEqual(json.loads(sent.data), REQUEST)
        self.assertEqual(sent.get_header("Authorization"), "Bearer " + TOKEN)

    def test_unverified_actor_is_denied(self):
        with self.assertRaises(WorkflowDenied):
            self.authorize({**VERIFIED, "authorized": False})

    def test_changed_step_is_denied(self):
        with self.assertRaises(WorkflowDenied):
            self.authorize({**VERIFIED, "step_id": "finance"})

    def test_changed_version_is_denied(self):
        with self.assertRaises(WorkflowDenied):
            self.authorize({**VERIFIED, "record_digest": "record-v2"})

    def test_wrong_company_is_denied(self):
        with self.assertRaises(WorkflowDenied):
            self.authorize({**VERIFIED, "company": "15"})

    def test_untrusted_phone_is_rejected(self):
        with self.assertRaises(IntegrationUnavailable):
            self.authorize({**VERIFIED, "phone_number": "invalid"})

    def test_extra_fields_are_rejected(self):
        with self.assertRaises(IntegrationUnavailable):
            self.authorize({**VERIFIED, "admin": True})

    def test_http_redirects_are_never_followed(self):
        from approval_gateway.integrations.generic import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(
            None, None, None, 302, "Moved", None, "https://elsewhere.example"))

    def test_missing_credentials_fail_configuration(self):
        with self.assertRaises(ValueError):
            build_adapter({
                "verification_url": "https://erp.example/api/approvals/verify",
                "token_env": "GENERIC_TOKEN"
            }, {})

    def test_http_non_loopback_rejected(self):
        with self.assertRaises(ValueError):
            GenericWorkflowGuard("http://erp.example/verify", TOKEN,
                                 allow_local_http=True)

    def test_http_403_denied(self):
        self.guard.opener = Mock()
        self.guard.opener.open.side_effect = urllib.error.HTTPError(
            self.guard.verify_url, 403, "Forbidden", {}, None)
        with self.assertRaises(WorkflowDenied):
            self.guard.authorize(REQUEST)


if __name__ == "__main__":
    unittest.main()
