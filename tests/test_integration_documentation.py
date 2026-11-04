"""ERP docs remain consolidated and locally linked."""
from pathlib import Path
import json
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/integration/README.md"
REMOVED = (
    "docs/integration/QUICKSTART.md",
    "docs/integration/LOCAL-SANDBOX.md",
    "docs/universal-erp-integration-v0.1.md",
    "REQUISITION_INTEGRATION_DEVELOPER_GUIDE.md",
)

class IntegrationDocsTests(unittest.TestCase):
    def test_canonical_document_is_only_integration_guide(self):
        self.assertTrue(DOC.is_file())
        self.assertIn("ERP Integration Developer Guide", DOC.read_text().splitlines()[0])
        self.assertIn("docs/integration/README.md", (ROOT / "README.md").read_text())
        for path in REMOVED:
            with self.subTest(path=path):
                self.assertFalse((ROOT / path).exists())
                self.assertNotIn(path, DOC.read_text())
                self.assertNotIn(path, (ROOT / "README.md").read_text())

    def test_local_links_exist(self):
        text = DOC.read_text()
        for link in re.findall(r"\]\(([^)]+)\)", text):
            if "://" in link or link.startswith("#"):
                continue
            destination = link.split("#", 1)[0]
            path = (DOC.parent / destination).resolve()
            with self.subTest(link=link):
                self.assertTrue(path.is_relative_to(ROOT))
                self.assertTrue(path.is_file())

    def test_machine_readable_contracts_remain(self):
        api = ROOT / "docs/integration/openapi/current-gateway.yaml"
        postman = ROOT / "docs/integration/postman/Universal-ERP-Approval.experimental.postman_collection.json"
        self.assertIn("/api/approval-requests:", api.read_text())
        self.assertIn("/api/identity-links:", api.read_text())
        self.assertTrue(json.loads(postman.read_text())["item"])

if __name__ == "__main__":
    unittest.main()
