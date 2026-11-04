"""Safety checks for operator ERP onboarding without touching production."""
import sqlite3
import unittest

from approval_gateway.provision_erp import provision, validate_callback_url
from approval_gateway.security import verify_api_key


class ErpProvisionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE api_clients (
                id INTEGER PRIMARY KEY, system_name TEXT UNIQUE NOT NULL,
                api_key_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE integrations (
                system_name TEXT PRIMARY KEY, callback_url TEXT NOT NULL,
                callback_secret TEXT NOT NULL
            );
        """)

    def test_provisions_credentials_and_callback(self):
        key, secret = provision(self.conn, "new_erp", "https://erp.example.com/callback")
        self.assertTrue(key.startswith("wha_"))
        self.assertGreaterEqual(len(secret), 32)
        client = self.conn.execute(
            "SELECT * FROM api_clients WHERE system_name='new_erp'"
        ).fetchone()
        self.assertTrue(verify_api_key(key, client["api_key_hash"]))
        self.assertNotEqual(key, client["api_key_hash"])
        integration = self.conn.execute(
            "SELECT * FROM integrations WHERE system_name='new_erp'"
        ).fetchone()
        self.assertEqual(integration["callback_secret"], secret)

    def test_refuses_duplicate_without_rotating(self):
        key, _ = provision(self.conn, "external", "https://erp.example.com/callback")
        self.conn.commit()
        with self.assertRaises(ValueError):
            provision(self.conn, "external", "https://erp.example.com/other")
        client = self.conn.execute(
            "SELECT * FROM api_clients WHERE system_name='external'"
        ).fetchone()
        self.assertTrue(verify_api_key(key, client["api_key_hash"]))

    def test_rejects_unsafe_callback_urls(self):
        for url in (
            "http://erp.example.com/hook",
            "https://user:pass@erp.example.com/hook",
            "https://erp.example.com/hook?token=secret",
            "https://erp.example.com/hook#fragment",
            "https://erp.example.com:8443/hook",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_callback_url(url)

    def test_refuses_bad_system_identifier(self):
        for system in ("", "PETA", "../erp", "new erp", "4erp"):
            with self.subTest(system=system), self.assertRaises(ValueError):
                provision(self.conn, system, "https://erp.example.com/callback")


if __name__ == "__main__":
    unittest.main()
