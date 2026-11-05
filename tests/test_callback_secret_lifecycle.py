"""Callback signing secret staging is safe and leaves audit without secrets."""
import sqlite3
import unittest

from approval_gateway.manage_callback_secret import (
    stage_callback_secret, activate_callback_secret, abort_callback_secret,
)


class CallbackSecretLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE api_clients(system_name TEXT PRIMARY KEY,active INTEGER);
            CREATE TABLE integrations(
                system_name TEXT PRIMARY KEY,callback_secret TEXT,callback_url TEXT
            );
            CREATE TABLE integration_callback_secret_staging(
                system_name TEXT PRIMARY KEY,next_secret TEXT,staged_at TEXT
            );
            CREATE TABLE integration_callback_secret_events(
                id INTEGER PRIMARY KEY,system_name TEXT,
                event_type TEXT,created_at TEXT
            );
            INSERT INTO api_clients VALUES ('external',1);
            INSERT INTO integrations VALUES
                ('external','ssssssssssssssssssssssssssssssss',
                 'https://erp.example.com/callback');
        """)
        self.old_secret = self.conn.execute(
            "SELECT callback_secret FROM integrations"
        ).fetchone()[0]

    def current(self):
        return self.conn.execute(
            "SELECT callback_secret FROM integrations"
        ).fetchone()[0]

    def test_stage_then_activate_switches_once(self):
        next_secret = stage_callback_secret(self.conn, "external")
        self.conn.commit()
        self.assertGreaterEqual(len(next_secret), 32)
        self.assertNotEqual(next_secret, self.old_secret)
        self.assertEqual(self.current(), self.old_secret)
        with self.assertRaises(ValueError):
            stage_callback_secret(self.conn, "external")
        activate_callback_secret(self.conn, "external")
        self.conn.commit()
        self.assertEqual(self.current(), next_secret)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM integration_callback_secret_staging"
            ).fetchone()[0], 0,
        )
        event_types = [
            row[0] for row in self.conn.execute(
                "SELECT event_type FROM integration_callback_secret_events ORDER BY id"
            )
        ]
        self.assertEqual(event_types, ["secret_staged", "secret_activated"])
        self.assertNotIn(next_secret, str(event_types))

    def test_abort_retains_old_secret(self):
        next_secret = stage_callback_secret(self.conn, "external")
        abort_callback_secret(self.conn, "external")
        self.conn.commit()
        self.assertEqual(self.current(), self.old_secret)
        self.assertNotEqual(self.current(), next_secret)
        with self.assertRaises(ValueError):
            activate_callback_secret(self.conn, "external")

    def test_disabled_or_unknown_client_cannot_rotate(self):
        with self.assertRaises(ValueError):
            stage_callback_secret(self.conn, "missing")
        self.conn.execute("UPDATE api_clients SET active=0")
        with self.assertRaises(ValueError):
            stage_callback_secret(self.conn, "external")

    def test_transaction_rollback_discards_staged_secret(self):
        self.conn.execute("BEGIN IMMEDIATE")
        stage_callback_secret(self.conn, "external")
        self.conn.rollback()
        self.assertEqual(self.current(), self.old_secret)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM integration_callback_secret_staging"
        ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
