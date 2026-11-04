"""Identity-link contract tests: ERP API + our verified WhatsApp phone path."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from approval_gateway.config import load_settings
from approval_gateway.db import connection, init_db
from approval_gateway.identity_link import start, receive, status
from approval_gateway.integrations.generic import GenericWorkflowGuard
from approval_gateway.integrations.registry import configure_workflow_guards
from approval_gateway.repository import create_api_client
from approval_gateway.service import ServiceError
from approval_gateway.upgrade import migrate

UUID = "0656c52c-970d-11f1-b365-6805cae19a58"
PHONE = "255700000001"


class IdentityLinkTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.settings = replace(
            load_settings(), app_env="testing",
            database_path=str(Path(t.name) / "gateway.db"),
            log_path=str(Path(t.name) / "gateway.log"),
        )
        init_db(self.settings)
        migrate(self.settings)
        configure_workflow_guards({
            "external": GenericWorkflowGuard(
                "https://erp.example.com/verify", "v" * 32
            )
        })
        self.addCleanup(lambda: configure_workflow_guards({}))
        self.data = {
            "company": "14", "actor_directory_uuid": UUID,
            "source_user_id": "7", "phone_number": PHONE,
        }
        with connection(self.settings) as conn:
            create_api_client(conn, "external", "external-key")
            create_api_client(conn, "other", "other-key")
            conn.execute(
                "INSERT INTO integration_company_scopes VALUES (?,?)", ("external", "14")
            )
            conn.execute(
                "INSERT INTO integration_company_scopes VALUES (?,?)", ("other", "99")
            )

    def send(self, instruction, phone=PHONE, message_id="wamid.test1"):
        return receive(self.settings, {
            "action": "conversation", "text": instruction,
            "phone": phone, "message_id": message_id,
            "reference": None, "context_id": None,
        })

    def test_prove_phone_once_then_status(self):
        challenge = start(self.settings, "external-key", self.data)
        self.assertEqual(challenge["status"], "pending_phone_verification")
        self.assertFalse(status(self.settings, "external-key", UUID, "14")["verified"])
        self.assertEqual(self.send(challenge["whatsapp_message"])["reason"], "phone_link_verified")
        self.assertTrue(status(self.settings, "external-key", UUID, "14")["verified"])
        self.assertEqual(self.send(challenge["whatsapp_message"], message_id="wamid.test2")["reason"],
                         "invalid_or_expired_link")
        self.assertEqual(self.send(challenge["whatsapp_message"])["reason"], "duplicate_message")
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM actor_bindings WHERE system_name='external'"
            ).fetchone()[0], 1)

    def test_wrong_phone_does_not_enroll(self):
        challenge = start(self.settings, "external-key", self.data)
        self.assertEqual(self.send(challenge["whatsapp_message"], phone="255700000002")["reason"],
                         "invalid_or_expired_link")
        self.assertFalse(status(self.settings, "external-key", UUID, "14")["verified"])

    def test_expired_link_is_rejected(self):
        challenge = start(self.settings, "external-key", self.data)
        with connection(self.settings) as conn:
            conn.execute("UPDATE identity_link_challenges SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.send(challenge["whatsapp_message"])["reason"], "invalid_or_expired_link")

    def test_new_challenge_invalidates_previous(self):
        first = start(self.settings, "external-key", self.data)
        second = start(self.settings, "external-key", self.data)
        self.assertNotEqual(first["whatsapp_message"], second["whatsapp_message"])
        self.assertEqual(self.send(first["whatsapp_message"])["reason"], "invalid_or_expired_link")
        self.assertEqual(self.send(second["whatsapp_message"], message_id="wamid.test2")["reason"],
                         "phone_link_verified")

    def test_other_erp_cannot_read_link(self):
        challenge = start(self.settings, "external-key", self.data)
        with self.assertRaises(ServiceError):
            status(self.settings, "other-key", UUID, "14")
        with self.assertRaises(ServiceError):
            start(self.settings, "other-key", self.data)

    def test_revoked_client_blocks_completion(self):
        challenge = start(self.settings, "external-key", self.data)
        with connection(self.settings) as conn:
            conn.execute("UPDATE api_clients SET active=0 WHERE system_name='external'")
        self.assertEqual(self.send(challenge["whatsapp_message"])["reason"], "inactive_integration")

    def test_existing_identity_cannot_be_silently_rebound(self):
        challenge = start(self.settings, "external-key", self.data)
        self.send(challenge["whatsapp_message"])
        with self.assertRaises(ServiceError) as exc:
            start(self.settings, "external-key", self.data)
        self.assertEqual(exc.exception.status, 409)

    def test_link_requests_are_rate_limited(self):
        for i in range(5):
            self.assertEqual(
                start(self.settings, "external-key", self.data)["status"],
                "pending_phone_verification",
            )
        with self.assertRaises(ServiceError) as exc:
            start(self.settings, "external-key", self.data)
        self.assertEqual(exc.exception.status, 429)

    def test_preexisting_binding_does_not_claim_otp_verification(self):
        with connection(self.settings) as conn:
            conn.execute(
                """INSERT INTO approvers
                   (name,role,company,phone_number,active,created_at,updated_at)
                   VALUES ('Manual','manager','14',?,1,'now','now')""",
                (PHONE,),
            )
            pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO actor_bindings
                   (system_name,company,directory_uuid,source_user_id,approver_id)
                   VALUES ('external','14',?,?,?)""",
                (UUID, "7", pid),
            )
        self.assertFalse(status(self.settings, "external-key", UUID, "14")["verified"])

    def test_unrelated_incoming_text_is_not_consumed(self):
        self.assertIsNone(self.send("Approve", message_id="wamid.other"))
        self.assertIsNone(self.send("MENU", message_id="wamid.menu"))


if __name__ == "__main__":
    unittest.main()
