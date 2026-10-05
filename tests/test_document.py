from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from approval_gateway.config import Settings
from approval_gateway.upload_document import upload_pdf
from approval_gateway.whatsapp import WhatsAppClient, WhatsAppError


class DocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        # Credentials are fake; all external requests are mocked.
        self.settings = Settings(
            app_env="testing",
            host="127.0.0.1",
            port=8088,
            database_path="unused.sqlite3",
            log_path="unused.log",
            public_base_url="http://127.0.0.1:8088",
            default_expiry_hours=24,
            callback_timeout_seconds=2,
            callback_secret="",
            meta_verify_token="test-verify-token",
            meta_app_secret="test-app-secret",
            whatsapp_access_token="fake-token",
            whatsapp_phone_number_id="123",
            whatsapp_api_version="v23.0",
            whatsapp_template_name="workflow_approval_document",
            whatsapp_template_language="en",
            dry_run_whatsapp=False,
        )

        self.details = {
            "document_media_id": "987",
            "document_filename": "REQ-9.pdf",
            "reference_id": "REQ-9",
            "workflow_step": "Finance verification",
            "requested_by": "John John",
            "details": "Workshop materials",
            "decision_scope": "All eligible items",
        }

    def send(self, client, details=None):
        return client.send_approval_request(
            "255787550399",
            "Company 77",
            "Review this requisition.",
            "AGR-ABC",
            self.details if details is None else details,
        )

    def test_workflow_document_and_button_mapping(self):
        with patch.object(
            WhatsAppClient,
            "_post_graph",
            return_value={"messages": [{"id": "wamid.1"}]},
        ) as post:
            message_id = self.send(WhatsAppClient(self.settings))

        self.assertEqual(message_id, "wamid.1")
        payload = post.call_args.args[0]
        components = payload["template"]["components"]

        self.assertEqual(payload["to"], "255787550399")
        self.assertEqual(
            components[0]["parameters"][0]["document"],
            {"id": "987", "filename": "REQ-9.pdf"},
        )
        self.assertEqual(
            [item["text"] for item in components[1]["parameters"]],
            [
                "Company 77",
                "REQ-9",
                "Finance verification",
                "John John",
                "Workshop materials",
                "All eligible items",
            ],
        )
        self.assertEqual(
            components[2]["parameters"][0]["payload"],
            "approve_AGR-ABC",
        )
        self.assertEqual(
            components[3]["parameters"][0]["payload"],
            "reject_AGR-ABC",
        )

    def test_missing_media_blocks_network_call(self):
        details = dict(self.details)
        details.pop("document_media_id")

        with patch.object(WhatsAppClient, "_post_graph") as post:
            with self.assertRaises(ValueError):
                self.send(WhatsAppClient(self.settings), details)

            post.assert_not_called()

    def test_invalid_filename_blocks_network_call(self):
        with patch.object(WhatsAppClient, "_post_graph") as post:
            with self.assertRaises(ValueError):
                self.send(
                    WhatsAppClient(self.settings),
                    dict(self.details, document_filename="../REQ-9.pdf"),
                )

            post.assert_not_called()

    def test_dry_run_validates_without_sending(self):
        client = WhatsAppClient(replace(self.settings, dry_run_whatsapp=True))

        with patch.object(WhatsAppClient, "_post_graph") as post:
            self.assertEqual(self.send(client), "dry-run-AGR-ABC")

            with self.assertRaises(ValueError):
                self.send(client, {})

            post.assert_not_called()

    def test_missing_message_acknowledgement_is_unknown(self):
        with patch.object(WhatsAppClient, "_post_graph", return_value={}):
            with self.assertRaises(WhatsAppError) as caught:
                self.send(WhatsAppClient(self.settings))

        self.assertTrue(caught.exception.delivery_unknown)

    def test_legacy_text_template_remains_supported(self):
        settings = replace(
            self.settings,
            whatsapp_template_name="approval_request",
        )

        with patch.object(
            WhatsAppClient,
            "_post_graph",
            return_value={"messages": [{"id": "wamid.2"}]},
        ) as post:
            self.send(WhatsAppClient(settings))

        components = post.call_args.args[0]["template"]["components"]
        self.assertEqual(len(components), 3)
        self.assertEqual(components[0]["type"], "body")

    def test_upload_pdf_multipart(self):
        # This minimal fixture tests transport construction, not PDF integrity.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approval.pdf"
            content = b"%PDF-1.4\ntransport-test"
            path.write_bytes(content)

            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = (
                b'{"id":"987"}'
            )

            with patch(
                "approval_gateway.upload_document.urllib.request.build_opener",
                return_value=opener,
            ):
                media_id = upload_pdf(self.settings, path)

        self.assertEqual(media_id, "987")
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/123/media"))
        self.assertIn(b"application/pdf", request.data)
        self.assertIn(content, request.data)

    def test_invalid_pdf_does_not_open_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.pdf"
            path.write_bytes(b"not a PDF")

            with patch(
                "approval_gateway.upload_document.urllib.request.build_opener"
            ) as build:
                with self.assertRaises(ValueError):
                    upload_pdf(self.settings, path)

                build.assert_not_called()

    def test_dry_run_blocks_real_upload(self):
        settings = replace(self.settings, dry_run_whatsapp=True)

        with patch(
            "approval_gateway.upload_document.urllib.request.build_opener"
        ) as build:
            with self.assertRaises(ValueError):
                upload_pdf(settings, "unused.pdf")

            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
