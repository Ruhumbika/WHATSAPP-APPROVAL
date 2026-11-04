"""Full HTTP contract: ERP request, gateway-owned signed Meta webhook, status."""
from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from approval_gateway.config import load_settings
from approval_gateway.db import connection, init_db
from approval_gateway.integrations.generic import GenericWorkflowGuard
from approval_gateway.integrations.registry import configure_workflow_guards
from approval_gateway.repository import create_api_client
from approval_gateway.server import Server
from approval_gateway.upgrade import migrate

ACTOR = "0656c52c-970d-11f1-b365-6805cae19a58"


class IdentityLinkHTTPTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.settings = replace(
            load_settings(),
            app_env="testing", host="127.0.0.1", port=0,
            database_path=str(Path(t.name) / "db.sqlite3"),
            log_path=str(Path(t.name) / "gateway.log"),
            meta_app_secret="meta-test-secret",
            whatsapp_phone_number_id="123456",
            dry_run_whatsapp=True,
        )
        init_db(self.settings)
        migrate(self.settings)
        configure_workflow_guards({
            "external": GenericWorkflowGuard(
                "https://erp.example.com/verify", "v" * 32
            )
        })
        self.addCleanup(lambda: configure_workflow_guards({}))
        with connection(self.settings) as conn:
            create_api_client(conn, "external", "gateway-key")
            conn.execute("INSERT INTO integration_company_scopes VALUES (?,?)",
                         ("external", "14"))
        self.server = Server(self.settings)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown_server)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def shutdown_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def fetch(self, method, path, payload=None, auth=True, meta=False, sig_ok=True):
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer gateway-key"
        if meta:
            signature = hmac.new(
                b"meta-test-secret", body, hashlib.sha256
            ).hexdigest()
            headers["X-Hub-Signature-256"] = "sha256=" + (
                signature if sig_ok else "0" * 64
            )
        request = urllib.request.Request(
            self.url + path, data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def message(self, code, phone="255700000001", receiver="123456"):
        return {
            "object": "whatsapp_business_account",
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "metadata": {"phone_number_id": receiver},
                        "messages": [{
                            "id": "wamid.link.001",
                            "from": phone,
                            "type": "text",
                            "text": {"body": code},
                        }],
                    }
                }]
            }],
        }

    def test_gateway_owned_whatsapp_completes_link(self):
        status, created = self.fetch("POST", "/api/identity-links", {
            "company": "14", "actor_directory_uuid": ACTOR,
            "source_user_id": "7", "phone_number": "255700000001",
        })
        self.assertEqual(status, 201)
        self.assertTrue(created["whatsapp_message"].startswith("LINK "))
        path = "/api/identity-links/" + quote(ACTOR) + "?company=14"
        self.assertEqual(self.fetch("GET", path)[1]["verified"], False)
        self.assertEqual(
            self.fetch("POST", "/webhook/whatsapp",
                       self.message(created["whatsapp_message"]), auth=False,
                       meta=True)[0],
            200,
        )
        self.assertEqual(self.fetch("GET", path)[1]["verified"], True)

    def test_revoke_link_over_http_then_verify_relink_is_required(self):
        created_status, created = self.fetch("POST", "/api/identity-links", {
            "company": "14", "actor_directory_uuid": ACTOR,
            "source_user_id": "7", "phone_number": "255700000001",
        })
        self.assertEqual(created_status, 201)
        self.assertEqual(
            self.fetch("POST", "/webhook/whatsapp",
                       self.message(created["whatsapp_message"]),
                       auth=False, meta=True)[0], 200
        )
        path = "/api/identity-links/" + quote(ACTOR) + "?company=14"
        self.assertTrue(self.fetch("GET", path)[1]["verified"])
        revoke_path = "/api/identity-links/" + quote(ACTOR) + "/revoke"
        code, response = self.fetch("POST", revoke_path, {
            "company": "14", "source_user_id": "7",
        })
        self.assertEqual(code, 200)
        self.assertEqual(response["status"], "revoked")
        self.assertFalse(self.fetch("GET", path)[1]["verified"])
        code, response = self.fetch("POST", revoke_path, {
            "company": "14", "source_user_id": "7",
        })
        self.assertEqual(code, 404)
        self.assertEqual(
            self.fetch("POST", "/api/identity-links", {
                "company": "14", "actor_directory_uuid": ACTOR,
                "source_user_id": "7", "phone_number": "255700000009",
            })[0], 201
        )

    def test_meta_signature_is_required_to_link(self):
        _, created = self.fetch("POST", "/api/identity-links", {
            "company": "14", "actor_directory_uuid": ACTOR,
            "source_user_id": "7", "phone_number": "255700000001",
        })
        status, _ = self.fetch(
            "POST", "/webhook/whatsapp", self.message(created["whatsapp_message"]),
            auth=False, meta=True, sig_ok=False,
        )
        self.assertEqual(status, 403)
        path = "/api/identity-links/" + quote(ACTOR) + "?company=14"
        self.assertFalse(self.fetch("GET", path)[1]["verified"])

    def test_wrong_gateway_phone_id_does_not_link(self):
        _, created = self.fetch("POST", "/api/identity-links", {
            "company": "14", "actor_directory_uuid": ACTOR,
            "source_user_id": "7", "phone_number": "255700000001",
        })
        self.assertEqual(
            self.fetch("POST", "/webhook/whatsapp",
                       self.message(created["whatsapp_message"], receiver="999"),
                       auth=False, meta=True)[0],
            200,
        )
        path = "/api/identity-links/" + quote(ACTOR) + "?company=14"
        self.assertFalse(self.fetch("GET", path)[1]["verified"])


if __name__ == "__main__":
    unittest.main()
