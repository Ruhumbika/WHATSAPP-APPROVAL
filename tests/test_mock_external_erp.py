"""Independent mock ERP contract tests without external network access."""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

MOCK = Path(__file__).resolve().parent.parent / (
    "docs/integration/examples/python/mock_erp_server.py"
)
spec = importlib.util.spec_from_file_location("sample_mock_erp", MOCK)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MockERPContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = str(Path(self.temp.name) / "erp.sqlite3")
        module.init_store(self.db_path)
        self.verification = {
            "company": "14", "reference_id": "LEAVE-2026-001",
            "request_type": "leave_approval", "step_id": "manager",
            "workflow_version": "record-v1",
            "actor_directory_uuid": module.ACTOR,
        }
        self.decision = {
            "event_id": "decision:AGR-SANDBOX-001",
            "source_system": "external", "company": "14",
            "reference_id": "LEAVE-2026-001",
            "step_id": "manager", "workflow_version": "record-v1",
            "status": "approved",
            "actor": {"directory_uuid": module.ACTOR, "user_id": "7"},
        }

    def test_verify_before_decision_then_reject_after_commit(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self.assertTrue(module.check_verification(conn, self.verification)["authorized"])
            applied = module.apply_decision(conn, self.decision)
            self.assertEqual(applied["execution_status"], "applied")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self.assertFalse(module.check_verification(conn, self.verification)["authorized"])
            replay = module.apply_decision(conn, self.decision)
            self.assertEqual(replay, applied)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT state FROM records").fetchone()[0], "approved")

    def test_wrong_company_never_executes(self):
        self.decision["company"] = "99"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            reply = module.apply_decision(conn, self.decision)
        self.assertEqual(reply["execution_status"], "rejected")
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT state FROM records").fetchone()[0], "pending")

    def test_stale_version_cannot_verify(self):
        self.verification["workflow_version"] = "older"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self.assertFalse(module.check_verification(conn, self.verification)["authorized"])

    def test_signature_authentication(self):
        import hashlib
        import hmac
        import time
        secret = "s" * 32
        timestamp = str(int(time.time()))
        body = b'{"event_id":"decision:AGR"}'
        signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body,
                             hashlib.sha256).hexdigest()
        self.assertTrue(module.verify_signature(body, timestamp, signature, secret))
        self.assertFalse(module.verify_signature(body + b" ", timestamp, signature, secret))
        self.assertFalse(module.verify_signature(body, str(int(timestamp) - 400), signature, secret))


if __name__ == "__main__":
    unittest.main()
