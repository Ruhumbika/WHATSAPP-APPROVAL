"""Credential lifecycle tests with a local SQLite database only."""
import sqlite3
import unittest

from approval_gateway.manage_erp_credentials import rotate_api_key, revoke_client
from approval_gateway.provision_erp import provision
from approval_gateway.repository import authenticate_client


class CredentialLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE api_clients (
                id INTEGER PRIMARY KEY,
                system_name TEXT UNIQUE NOT NULL,
                api_key_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE integrations (
                system_name TEXT PRIMARY KEY,
                callback_url TEXT NOT NULL,
                callback_secret TEXT NOT NULL
            );
            CREATE TABLE integration_company_scopes (
                system_name TEXT NOT NULL,
                company TEXT NOT NULL,
                PRIMARY KEY (system_name, company)
            );
            CREATE TABLE api_client_key_overlaps (
                system_name TEXT PRIMARY KEY,
                previous_key_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE integration_credential_events (
                id INTEGER PRIMARY KEY,
                system_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        self.old_key, self.callback_secret = provision(
            self.conn, "external", "https://erp.example.com/callback", ("14",)
        )
        self.conn.commit()

    def test_key_rotation_invalidates_previous_credential(self):
        self.conn.execute("BEGIN IMMEDIATE")
        new_key = rotate_api_key(self.conn, "external")
        self.conn.commit()
        self.assertNotEqual(new_key, self.old_key)
        self.assertIsNone(authenticate_client(self.conn, self.old_key))
        self.assertEqual(authenticate_client(self.conn, new_key)["system_name"], "external")
        event = self.conn.execute(
            "SELECT event_type FROM integration_credential_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event["event_type"], "api_key_rotated")
        callback = self.conn.execute(
            "SELECT callback_secret FROM integrations WHERE system_name=?", ("external",)
        ).fetchone()
        self.assertEqual(callback["callback_secret"], self.callback_secret)

    def test_staged_rotation_allows_both_then_expires_old_key(self):
        self.conn.execute("BEGIN IMMEDIATE")
        new_key = rotate_api_key(self.conn, "external", overlap_minutes=15)
        self.conn.commit()
        self.assertIsNotNone(authenticate_client(self.conn, new_key))
        self.assertIsNotNone(authenticate_client(self.conn, self.old_key))
        with self.assertRaises(ValueError):
            rotate_api_key(self.conn, "external", overlap_minutes=15)
        self.conn.execute(
            "UPDATE api_client_key_overlaps SET expires_at=? WHERE system_name='external'",
            ("2000-01-01T00:00:00+00:00",),
        )
        self.conn.commit()
        self.assertIsNone(authenticate_client(self.conn, self.old_key))
        self.assertIsNotNone(authenticate_client(self.conn, new_key))

    def test_revocation_invalidates_overlap_and_current_key(self):
        self.conn.execute("BEGIN IMMEDIATE")
        new_key = rotate_api_key(self.conn, "external", overlap_minutes=30)
        self.conn.commit()
        self.assertIsNotNone(authenticate_client(self.conn, self.old_key))
        self.conn.execute("BEGIN IMMEDIATE")
        revoke_client(self.conn, "external")
        self.conn.commit()
        self.assertIsNone(authenticate_client(self.conn, self.old_key))
        self.assertIsNone(authenticate_client(self.conn, new_key))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM api_client_key_overlaps"
        ).fetchone()[0], 0)

    def test_rejects_invalid_rotation_windows(self):
        for minutes in (-1, 1441, 1.5, True):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                rotate_api_key(self.conn, "external", overlap_minutes=minutes)

    def test_revocation_disables_all_current_credentials(self):
        self.conn.execute("BEGIN IMMEDIATE")
        revoke_client(self.conn, "external")
        self.conn.commit()
        self.assertIsNone(authenticate_client(self.conn, self.old_key))
        with self.assertRaises(ValueError):
            rotate_api_key(self.conn, "external")
        event = self.conn.execute(
            "SELECT event_type FROM integration_credential_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event["event_type"], "client_revoked")

    def test_cannot_rotate_nonexistent_or_revoked_system(self):
        with self.assertRaises(ValueError):
            rotate_api_key(self.conn, "absent")
        self.conn.execute("BEGIN IMMEDIATE")
        revoke_client(self.conn, "external")
        self.conn.commit()
        with self.assertRaises(ValueError):
            revoke_client(self.conn, "external")

    def test_rotation_rollback_preserves_previous_key(self):
        self.conn.execute("BEGIN IMMEDIATE")
        rotate_api_key(self.conn, "external")
        self.conn.rollback()
        self.assertIsNotNone(authenticate_client(self.conn, self.old_key))
        count = self.conn.execute(
            "SELECT COUNT(*) FROM integration_credential_events"
        ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
