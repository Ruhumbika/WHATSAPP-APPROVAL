"""Read-only readiness reporting never claims external verification."""
import sqlite3
import unittest

from approval_gateway.integration_readiness import inspect_integration


class IntegrationReadinessTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE api_clients(system_name TEXT PRIMARY KEY, active INTEGER);
            CREATE TABLE integrations(system_name TEXT PRIMARY KEY, callback_url TEXT);
            CREATE TABLE integration_company_scopes(
                system_name TEXT, company TEXT,
                PRIMARY KEY(system_name, company));
            CREATE TABLE approvers(id INTEGER PRIMARY KEY, active INTEGER);
            CREATE TABLE actor_bindings(
                system_name TEXT, approver_id INTEGER, active INTEGER);
        """)

    def test_missing_integration_is_not_ready(self):
        report = inspect_integration(self.conn, "unknown")
        self.assertFalse(report["configuration_complete"])
        self.assertFalse(report["integration_ready"])

    def test_populated_configuration_still_requires_external_acceptance(self):
        self.conn.execute("INSERT INTO api_clients VALUES ('external', 1)")
        self.conn.execute(
            "INSERT INTO integrations VALUES ('external','https://erp.example.com/callback')")
        self.conn.execute("INSERT INTO integration_company_scopes VALUES ('external','14')")
        self.conn.execute("INSERT INTO approvers VALUES (1,1)")
        self.conn.execute("INSERT INTO actor_bindings VALUES ('external',1,1)")
        report = inspect_integration(self.conn, "external")
        self.assertTrue(report["configuration_complete"])
        self.assertFalse(report["integration_ready"])
        self.assertEqual(report["allowed_companies"], ["14"])
        self.assertEqual(report["active_actor_binding_count"], 1)

    def test_disabled_client_blocks_configuration(self):
        self.conn.execute("INSERT INTO api_clients VALUES ('external', 0)")
        report = inspect_integration(self.conn, "external")
        self.assertFalse(report["configuration_checks"]["active_client"])
        self.assertFalse(report["integration_ready"])


if __name__ == "__main__":
    unittest.main()
