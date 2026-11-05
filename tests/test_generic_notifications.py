"""Generic ERP WhatsApp notification copy must not reference PETA."""
import sqlite3
import unittest
from datetime import datetime, timezone

from approval_gateway.notifications import (
    enqueue_notification, enqueue_execution_failure,
    enqueue_repeat_notification,
)


class NotificationERPNeutralTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE decision_notifications (
                id INTEGER PRIMARY KEY,
                approval_request_id INTEGER, kind TEXT, phone TEXT, body TEXT,
                next_attempt_at TEXT, expires_at TEXT, created_at TEXT,
                updated_at TEXT, UNIQUE(approval_request_id, kind)
            );
            CREATE TABLE decision_repeat_notifications (
                id INTEGER PRIMARY KEY, approval_request_id INTEGER,
                inbound_message_id TEXT UNIQUE, kind TEXT, phone TEXT,
                body TEXT, next_attempt_at TEXT, expires_at TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE callback_attempts (
                approval_request_id INTEGER, status TEXT
            );
        """)
        self.now = datetime.now(timezone.utc).isoformat()
        self.task = {
            "id": 3, "source_system": "ghrm", "company": "14",
            "source_reference_id": "LEAVE-2026-001",
            "request_type": "leave_approval",
            "actor_phone": "255700000001",
            "status": "approved", "execution_status": "pending",
            "responded_at": self.now,
            "payload_json": "{}",
            "decision_rejection_json": None,
        }

    def test_generic_received_and_result_do_not_mention_peta(self):
        enqueue_notification(self.conn, self.task, "received", self.now)
        rows = self.conn.execute("SELECT body FROM decision_notifications").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("GHRM", rows[0]["body"])
        self.assertIn("leave request", rows[0]["body"])
        self.assertNotIn("PETA", rows[0]["body"])
        self.task["execution_status"] = "applied"
        enqueue_notification(self.conn, self.task, "result", self.now)
        bodies = [row["body"] for row in self.conn.execute(
            "SELECT body FROM decision_notifications ORDER BY id"
        )]
        self.assertEqual(len(bodies), 2)
        self.assertIn("confirmed", bodies[1])
        self.assertTrue(all("PETA" not in body for body in bodies))

    def test_generic_repeated_decision_uses_correct_source(self):
        enqueue_repeat_notification(
            self.conn, self.task,
            {"action": "approved", "message_id": "wamid.repeat"},
            self.now,
        )
        body = self.conn.execute(
            "SELECT body FROM decision_repeat_notifications"
        ).fetchone()["body"]
        self.assertIn("GHRM", body)
        self.assertNotIn("PETA", body)

    def test_callback_failure_mentions_erp_source(self):
        enqueue_execution_failure(self.conn, self.task, self.now)
        body = self.conn.execute(
            "SELECT body FROM decision_repeat_notifications"
        ).fetchone()["body"]
        self.assertIn("GHRM", body)
        self.assertNotIn("PETA", body)

    def test_peta_legacy_wording_is_preserved(self):
        self.task.update(
            source_system="peta", request_type="requisition_approval",
            source_reference_id="293",
        )
        enqueue_notification(self.conn, self.task, "received", self.now)
        body = self.conn.execute(
            "SELECT body FROM decision_notifications"
        ).fetchone()["body"]
        self.assertIn("PETA confirmation", body)
        self.assertIn("requisition", body)


if __name__ == "__main__":
    unittest.main()
