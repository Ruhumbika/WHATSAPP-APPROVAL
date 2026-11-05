"""Sandbox bootstrap must be isolated from the normal gateway."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from approval_gateway.config import load_settings
from approval_gateway.db import connection
from approval_gateway.local_sandbox import create_local_sandbox
from approval_gateway.repository import authenticate_client


class LocalSandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "sandbox"

    def test_new_sandbox_is_private_and_dry_run(self):
        report = create_local_sandbox(self.root, 8098)
        self.assertEqual(report["directory"], str(self.root))
        env_path = Path(report["env_file"])
        env_text = env_path.read_text()
        self.assertIn("DRY_RUN_WHATSAPP=true", env_text)
        self.assertIn("APP_HOST=127.0.0.1", env_text)
        self.assertIn("APP_ENV=testing", env_text)
        self.assertIn("WHATSAPP_ACCESS_TOKEN=\n", env_text)
        self.assertIn("CALLBACK_SECRET=\n", env_text)
        self.assertIn("WORKFLOW_NOTIFICATIONS_ENABLED=false", env_text)
        self.assertIn("WHATSAPP_ADMIN_PHONES=\n", env_text)
        self.assertNotIn(report["api_key"], env_text)
        self.assertNotIn(report["callback_secret"], env_text)
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(env_path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.root / "sandbox.sqlite3").st_mode & 0o777, 0o600)
        import sqlite3
        with sqlite3.connect(str(self.root / "sandbox.sqlite3")) as conn:
            conn.row_factory = sqlite3.Row
            client = authenticate_client(conn, report["api_key"])
            self.assertEqual(client["system_name"], "sandbox_erp")
            self.assertEqual(conn.execute(
                "SELECT company FROM integration_company_scopes"
            ).fetchone()[0], "sandbox")
            self.assertEqual(conn.execute(
                "SELECT callback_secret FROM integrations"
            ).fetchone()[0], report["callback_secret"])

    def test_bootstrapped_sandbox_serves_isolated_http_contract(self):
        import json
        import threading
        import urllib.request
        from dataclasses import replace
        from approval_gateway.server import Server

        report = create_local_sandbox(self.root, 8098)
        values = dict(
            line.split("=", 1)
            for line in Path(report["env_file"]).read_text().splitlines()
            if line and not line.startswith("#")
        )
        with patch.dict(os.environ, values):
            loaded = load_settings()
        self.assertTrue(loaded.dry_run_whatsapp)
        self.assertEqual(loaded.host, "127.0.0.1")
        self.assertEqual(loaded.whatsapp_access_token, "")
        self.assertEqual(loaded.meta_app_secret, "")
        with Server(replace(loaded, port=0)) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = "http://127.0.0.1:" + str(server.server_port)
                with urllib.request.urlopen(base + "/health", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                request = urllib.request.Request(
                    base + "/api/integration-status",
                    headers={"Authorization": "Bearer " + report["api_key"]},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    details = json.load(response)
                self.assertEqual(details["system"], "sandbox_erp")
                self.assertEqual(details["allowed_companies"], ["sandbox"])
                self.assertFalse(details["configuration_complete"])
                self.assertFalse(details["integration_ready"])
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_refuses_overwrite_and_invalid_port(self):
        with self.assertRaises(ValueError):
            create_local_sandbox(self.root, 80)
        create_local_sandbox(self.root)
        with self.assertRaises(ValueError):
            create_local_sandbox(self.root)
        with self.assertRaises(ValueError):
            create_local_sandbox(self.root, 9000)

    def test_refuses_production_environment(self):
        with patch("approval_gateway.local_sandbox.load_settings") as settings:
            settings.return_value.app_env = "production"
            with self.assertRaises(ValueError):
                create_local_sandbox(self.root)
        self.assertFalse(self.root.exists())

    def test_rejects_relative_or_symlink_directory(self):
        with self.assertRaises(ValueError):
            create_local_sandbox(Path("relative-path"))
        link = Path(self.tmp.name) / "sandbox-link"
        link.symlink_to(Path(self.tmp.name), target_is_directory=True)
        with self.assertRaises(ValueError):
            create_local_sandbox(link)


if __name__ == "__main__":
    unittest.main()
