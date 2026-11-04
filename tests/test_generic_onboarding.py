"""Operator self-contained generic onboarding config tests."""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from approval_gateway.config import load_settings
from approval_gateway.configure_generic_erp import add_generic_adapter
from approval_gateway.db import connection, init_db
from approval_gateway.integrations.bootstrap import bootstrap_integrations
from approval_gateway.integrations.registry import configure_workflow_guards, get_workflow_guard
from approval_gateway.integrations.generic import GenericWorkflowGuard
from approval_gateway.provision_erp import provision
from approval_gateway.upgrade import migrate


class OperatorOnboardingTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        root = Path(t.name)
        self.path = root / "integrations.json"
        self.path.write_text(
            json.dumps({"systems": {
                "peta": {"enabled": False, "factory": "peta:build_adapter", "options": {}}
            }}),
            encoding="utf-8",
        )
        self.settings = replace(
            load_settings(),
            app_env="testing",
            database_path=str(root / "db.sqlite3"),
            log_path=str(root / "gateway.log"),
            integrations_config_path=str(self.path),
        )
        init_db(self.settings)
        migrate(self.settings)
        with connection(self.settings) as conn:
            provision(
                conn, "external", "https://erp.example.com/callback", ("14",)
            )
        self.addCleanup(lambda: configure_workflow_guards({}))

    def test_operator_registers_generic_adapter_without_code_changes(self):
        with patch.dict(os.environ, {"EXT_VERIFY_TOKEN": "v" * 40}):
            add_generic_adapter(
                self.settings, "external", "https://erp.example.com/verify",
                "EXT_VERIFY_TOKEN",
            )
            loaded = bootstrap_integrations(self.settings)
        self.assertEqual(loaded, ("external",))
        self.assertIsInstance(get_workflow_guard("external"), GenericWorkflowGuard)
        content = self.path.read_text()
        self.assertNotIn("v" * 40, content)
        self.assertIn("generic:build_adapter", content)

    def test_existing_adapter_never_overwritten(self):
        with patch.dict(os.environ, {"EXT_VERIFY_TOKEN": "v" * 40}):
            add_generic_adapter(
                self.settings, "external", "https://erp.example.com/verify",
                "EXT_VERIFY_TOKEN",
            )
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                add_generic_adapter(
                    self.settings, "external", "https://other.example.com/verify",
                    "EXT_VERIFY_TOKEN",
                )
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_credential_refuses_and_preserves_configuration(self):
        original = self.path.read_bytes()
        with patch.dict(os.environ, {"EXT_VERIFY_TOKEN": ""}):
            with self.assertRaises(ValueError):
                add_generic_adapter(
                    self.settings, "external", "https://erp.example.com/verify",
                    "EXT_VERIFY_TOKEN",
                )
        self.assertEqual(self.path.read_bytes(), original)

    def test_unregistered_client_cannot_be_configured(self):
        with patch.dict(os.environ, {"EXT_VERIFY_TOKEN": "v" * 40}):
            with self.assertRaises(ValueError):
                add_generic_adapter(
                    self.settings, "unknown", "https://erp.example.com/verify",
                    "EXT_VERIFY_TOKEN",
                )


if __name__ == "__main__":
    unittest.main()
