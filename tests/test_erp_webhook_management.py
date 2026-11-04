"""Webhook URL changes cannot lose pending approvals or callback executions."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from approval_gateway.config import load_settings
from approval_gateway.db import connection, init_db
from approval_gateway.manage_erp_webhook import update_callback_url
from approval_gateway.provision_erp import provision
from approval_gateway.upgrade import migrate


class WebhookLifecycleTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.settings = replace(
            load_settings(), app_env="testing",
            database_path=str(Path(root.name) / "gateway.db"),
            log_path=str(Path(root.name) / "gateway.log"),
        )
        init_db(self.settings)
        migrate(self.settings)
        with connection(self.settings) as conn:
            provision(conn, "external", "https://old.example.com/approval", ("14",))
            conn.execute(
                """INSERT INTO approvers
                   (id,name,role,company,phone_number,active,created_at,updated_at)
                   VALUES (1,'Approver','manager','14','255700000001',1,'now','now')"""
            )

    def add_task(self, status="pending", execution_status="not_requested"):
        with connection(self.settings) as conn:
            conn.execute(
                """INSERT INTO approval_requests
                  (gateway_reference_id,source_reference_id,source_system,
                   request_type,company,message,approver_id,callback_url,
                   status,execution_status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "AGR-TEST1234", "LEAVE-1", "external",
                    "leave_approval", "14", "Leave review", 1,
                    "https://old.example.com/approval", status,
                    execution_status, "now", "now",
                ),
            )

    def change(self, url="https://new.example.com/approval"):
        with connection(self.settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            update_callback_url(conn, "external", url)

    def test_updates_callback_and_audits(self):
        self.change()
        with connection(self.settings) as conn:
            row = conn.execute(
                "SELECT callback_url FROM integrations WHERE system_name='external'"
            ).fetchone()
            self.assertEqual(row["callback_url"], "https://new.example.com/approval")
            event = conn.execute("SELECT event_type FROM integration_webhook_events").fetchone()
            self.assertEqual(event["event_type"], "callback_url_updated")

    def test_pending_approval_blocks_change(self):
        self.add_task()
        with self.assertRaises(ValueError):
            self.change()
        with connection(self.settings) as conn:
            url = conn.execute("SELECT callback_url FROM integrations").fetchone()[0]
            self.assertEqual(url, "https://old.example.com/approval")

    def test_unacknowledged_decision_blocks_change(self):
        self.add_task("approved", "pending")
        with self.assertRaises(ValueError):
            self.change()
        with connection(self.settings) as conn:
            conn.execute(
                "UPDATE approval_requests SET execution_status='applied'"
            )
        self.change()

    def test_rejects_insecure_or_duplicate_endpoint(self):
        for new in (
            "http://new.example.com/approval",
            "https://new.example.com/approval?secret=1",
            "https://old.example.com/approval",
        ):
            with self.subTest(new=new), self.assertRaises(ValueError):
                self.change(new)

    def test_unknown_or_disabled_client_denied(self):
        with connection(self.settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                update_callback_url(conn, "missing", "https://new.example.com/hook")
        with connection(self.settings) as conn:
            conn.execute("UPDATE api_clients SET active=0 WHERE system_name='external'")
        with self.assertRaises(ValueError):
            self.change()


if __name__ == "__main__":
    unittest.main()
