from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from approval_gateway.config import Settings
from approval_gateway.db import connection, init_db
from approval_gateway.repository import create_api_client, upsert_approver
from approval_gateway.service import ServiceError, create, decide, read_request
from approval_gateway.upgrade import migrate
from approval_gateway.integrations.permissions import WorkflowActor, WorkflowDenied
from approval_gateway.integrations.registry import configure_workflow_guards

class FixtureWorkflowGuard:
    """Model independent ERP authority without reading live integration settings."""

    def __init__(self, actor_uuid: str, phone: str, source_user_id):
        self.actor_uuid = actor_uuid
        self.phone = phone
        self.source_user_id = source_user_id
        self.step_id = 'finance_verification'
        self.record_digest = '1'

    def authorize(self, request):
        expected = {'request_type': 'requisition_approval', 'reference_id': 'REQ-9', 'actor_directory_uuid': self.actor_uuid, 'step_id': self.step_id}
        if request.get('company') not in {'77', '88'} or any((request.get(key) != value for key, value in expected.items())):
            raise WorkflowDenied("Request does not match the fixture's ERP workflow")
        return WorkflowActor(directory_uuid=self.actor_uuid, source_user_id=self.source_user_id, phone_number=self.phone, module_id='fixture-requisition', step_id=self.step_id, record_digest=self.record_digest)

    def prepare_delivery(self, settings, request):
        return json.loads(request['payload_json'])

class ActorBindingTests(unittest.TestCase):

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = Settings(app_env='testing', host='127.0.0.1', port=8088, database_path=str(root / 'gateway.sqlite3'), log_path=str(root / 'gateway.log'), public_base_url='http://127.0.0.1:8088', default_expiry_hours=24, callback_timeout_seconds=2, callback_secret='', meta_verify_token='test-verify-token', meta_app_secret='test-app-secret', whatsapp_access_token='test-access-token', whatsapp_phone_number_id='123', whatsapp_api_version='v23.0', whatsapp_template_name='workflow_approval_document', whatsapp_template_language='en', dry_run_whatsapp=True)
        self.actor_uuid = '0656c52c-970d-11f1-b365-6805cae19a58'
        self.phone = '255787550399'
        self.workflow = FixtureWorkflowGuard(self.actor_uuid, self.phone, None)
        configure_workflow_guards({'peta': self.workflow})
        self.addCleanup(configure_workflow_guards, {})
        init_db(self.settings)
        migrate(self.settings)
        with connection(self.settings) as conn:
            conn.execute('BEGIN IMMEDIATE')
            create_api_client(conn, 'peta', 'test-peta-key')
            upsert_approver(conn, 'John John', 'Requisition reviewer', '77', self.phone)
            self.approver_id = conn.execute('SELECT id FROM approvers').fetchone()['id']
            cursor = conn.execute('INSERT INTO actor_bindings (\n                    system_name, company, directory_uuid, source_user_id, approver_id, active\n                ) VALUES (?, ?, ?, NULL, ?, 1)', ('peta', '77', self.actor_uuid, self.approver_id))
            self.binding_id = cursor.lastrowid
            conn.execute('INSERT INTO integrations (system_name, callback_url, callback_secret)\n                   VALUES (?, ?, ?)', ('peta', 'http://127.0.0.1:1/callback', 's' * 32))
        self.data = {'request_type': 'requisition_approval', 'company': '77', 'reference_id': 'REQ-9', 'message': 'Review this requisition.', 'step_id': 'finance_verification', 'workflow_version': '1', 'actor_directory_uuid': self.actor_uuid, 'payload': {}}

    def new_request(self):
        return create(self.settings, 'test-peta-key', self.data)[0]

    def response(self, request):
        return {'action': 'approved', 'reference': request['gateway_reference_id'], 'context_id': None, 'phone': self.phone, 'message_id': 'incoming-1', 'text': 'Approve'}

    def assert_decision_denied(self, request):
        with self.assertRaises(ServiceError) as caught:
            decide(self.settings, self.response(request))
        self.assertEqual(caught.exception.status, 403)
        stored = read_request(self.settings, 'test-peta-key', request['gateway_reference_id'])
        self.assertEqual(stored['status'], 'pending')
        with connection(self.settings) as conn:
            count = conn.execute('SELECT COUNT(*) FROM callback_attempts WHERE approval_request_id = ?', (request['id'],)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_callback_allows_missing_local_user_mapping(self):
        request = self.new_request()
        self.assertTrue(decide(self.settings, self.response(request))['processed'])
        with connection(self.settings) as conn:
            row = conn.execute('SELECT payload_json FROM callback_attempts WHERE approval_request_id = ?', (request['id'],)).fetchone()
        event = json.loads(row['payload_json'])
        self.assertEqual(event['actor']['directory_uuid'], self.actor_uuid)
        self.assertIsNone(event['actor']['user_id'])
        self.assertEqual(event['company'], '77')

    def test_binding_cannot_be_used_for_another_company(self):
        with self.assertRaises(ServiceError) as caught:
            create(self.settings, 'test-peta-key', dict(self.data, company='88'))
        self.assertEqual(caught.exception.status, 403)

    def test_revoked_binding_blocks_decision(self):
        request = self.new_request()
        with connection(self.settings) as conn:
            conn.execute('UPDATE actor_bindings SET active = 0 WHERE id = ?', (self.binding_id,))
        self.assert_decision_denied(request)

    def test_changed_local_mapping_blocks_decision(self):
        request = self.new_request()
        with connection(self.settings) as conn:
            conn.execute("UPDATE actor_bindings SET source_user_id = '99' WHERE id = ?", (self.binding_id,))
        self.assert_decision_denied(request)

    def test_changed_phone_blocks_decision_from_old_number(self):
        request = self.new_request()
        with connection(self.settings) as conn:
            conn.execute('UPDATE approvers SET phone_number = ? WHERE id = ?', ('255711111111', self.approver_id))
        self.assert_decision_denied(request)

    def test_disabled_approver_blocks_decision(self):
        request = self.new_request()
        with connection(self.settings) as conn:
            conn.execute('UPDATE approvers SET active = 0 WHERE id = ?', (self.approver_id,))
        self.assert_decision_denied(request)
if __name__ == '__main__':
    unittest.main()
