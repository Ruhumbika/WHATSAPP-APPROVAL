"""Generic ERP deliveries must use our provider template, not PETA PDF fields."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from approval_gateway.config import load_settings
from approval_gateway.whatsapp import WhatsAppClient
from approval_gateway.worker import _send_delivery
from approval_gateway.integrations.generic import GenericWorkflowGuard
from approval_gateway.integrations.permissions import WorkflowActor
from approval_gateway.integrations.registry import configure_workflow_guards
from approval_gateway.db import connection, init_db
from approval_gateway.upgrade import migrate
from approval_gateway.repository import create_api_client
from approval_gateway.service import create

ACTOR = "0656c52c-970d-11f1-b365-6805cae19a58"


class GenericWhatsAppTemplateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = replace(
            load_settings(), app_env="testing",
            database_path=str(Path(temp.name) / "db.sqlite3"),
            log_path=str(Path(temp.name) / "gateway.log"),
            whatsapp_template_name="workflow_approval_detail_document_v2",
            dry_run_whatsapp=True,
        )
        init_db(self.settings)
        migrate(self.settings)
        self.guard = GenericWorkflowGuard("https://erp.example.com/verify", "x" * 40)
        configure_workflow_guards({"ghrm": self.guard})
        self.addCleanup(lambda: configure_workflow_guards({}))
        with connection(self.settings) as conn:
            create_api_client(conn, "ghrm", "ghrm-key")
            conn.execute(
                "INSERT INTO integrations VALUES (?,?,?)",
                ("ghrm", "https://erp.example.com/callback", "s" * 32),
            )
            conn.execute(
                "INSERT INTO integration_company_scopes VALUES (?,?)",
                ("ghrm", "14"),
            )
            conn.execute(
                """INSERT INTO approvers
                (id,name,role,company,phone_number,active,created_at,updated_at)
                VALUES (1,'Approver','reviewer','14','255700000001',1,'now','now')"""
            )
            conn.execute(
                """INSERT INTO actor_bindings
                (system_name,company,directory_uuid,source_user_id,approver_id,active)
                VALUES ('ghrm','14',?,?,1,1)""",
                (ACTOR, "7"),
            )

    def test_generic_template_does_not_require_peta_pdf(self):
        client = WhatsAppClient(self.settings)
        with self.assertRaises(ValueError):
            client.send_approval_request(
                "255700000001", "14", "Review leave", "AGR-AABBCC", {}
            )
        result = client.send_approval_request(
            "255700000001", "14", "Review leave", "AGR-AABBCC",
            {}, template_name="approval_request",
        )
        self.assertEqual(result, "dry-run-AGR-AABBCC")

    def test_worker_selects_generic_template(self):
        actor = WorkflowActor(
            directory_uuid=ACTOR, source_user_id="7",
            phone_number="255700000001", module_id="leave",
            step_id="manager", record_digest="v1",
        )
        with patch.object(self.guard, "authorize", return_value=actor):
            task, created = create(self.settings, "ghrm-key", {
                "source_system": "ghrm", "company": "14",
                "request_type": "leave_approval", "reference_id": "LEAVE-1",
                "step_id": "manager", "workflow_version": "v1",
                "actor_directory_uuid": ACTOR, "message": "Review leave",
                "payload": {"employee_id": "7"},
            })
            claimed = dict(task, delivery_attempts=1, delivery_status="sending")
            with connection(self.settings) as conn:
                conn.execute(
                    "UPDATE approval_requests SET delivery_status='sending',"
                    "delivery_attempts=1 WHERE id=?", (task["id"],)
                )
            mock_client = Mock()
            mock_client.send_approval_request.return_value = "wamid.success"
            _send_delivery(self.settings, mock_client, claimed)
        self.assertEqual(
            mock_client.send_approval_request.call_args.kwargs["template_name"],
            "approval_request",
        )
        with connection(self.settings) as conn:
            state = conn.execute(
                "SELECT delivery_status FROM approval_requests WHERE id=?",
                (task["id"],),
            ).fetchone()[0]
            self.assertEqual(state, "sent")


if __name__ == "__main__":
    unittest.main()
