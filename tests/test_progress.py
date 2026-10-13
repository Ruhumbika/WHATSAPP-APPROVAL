import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import test_gateway as baseline
from approval_gateway.db import connection
from approval_gateway.progress import handle_progress, render_progress
from approval_gateway.service import ServiceError

class ProgressTests(unittest.TestCase):
    setUp = baseline.GatewayTests.setUp
    new_request = baseline.GatewayTests.new_request

    def fixture(self):
        task = self.new_request()
        with connection(self.settings) as c:
            c.execute("UPDATE approval_requests SET source_reference_id='297' WHERE id=?", (task['id'],))
            binding = dict(c.execute('SELECT b.*, a.phone_number FROM actor_bindings b JOIN approvers a ON a.id=b.approver_id').fetchone())
        level = {'id': 2, 'level_id': 2, 'is_active': True, 'approval_status': 'PENDING',
                 'role': {'name':'CHECK'}, 'position': {'role_name':'HR'},
                 'approvers':[{'id':152, 'username':'john.john'}]}
        self.data = {'request_type':'requisition_approval', 'reference_id':'297', 'status':'In progress',
            'completed_steps':1, 'total_steps':2, 'remaining_steps':1, 'current_step':level,
            'following_step':None, 'latest_action':None, 'waiting_since':None,
            'levels':[level], 'updated_at':'2026-10-12T00:00:00+00:00'}
        directory = SimpleNamespace(get_service_details=lambda **kw:(object(), {'phone_number':binding['phone_number']}),
                                    source_user_id=lambda user:int(binding['source_user_id']))
        guard = SimpleNamespace(directory=directory, _international_phone=lambda p:p,
            peta=SimpleNamespace(base_url='http://127.0.0.1', document_access_token='x'*32, timeout_seconds=5))
        self.guard = guard
        self.reply = {'text':'STATUS REQ 297','phone':binding['phone_number'], 'message_id':'status-1','timestamp':str(int(time.time()))}
        return task

    def queue_count(self):
        with connection(self.settings) as c:
            return c.execute('SELECT COUNT(*) FROM decision_repeat_notifications').fetchone()[0]

    def test_live_status_is_durable_and_duplicate_is_not_resent(self):
        self.fixture()
        with patch('approval_gateway.progress.get_workflow_guard', return_value=self.guard), patch('approval_gateway.progress.PetaClient._get', return_value={'success':True,'data':self.data}):
            self.assertTrue(handle_progress(self.settings,self.reply)['processed'])
            handle_progress(self.settings,self.reply)
        self.assertEqual(self.queue_count(),1)

    def test_wrong_phone_cannot_read_request(self):
        self.fixture(); self.reply['phone']='255700000099'
        with self.assertRaises(ServiceError): handle_progress(self.settings,self.reply)
        self.assertEqual(self.queue_count(),0)

    def test_changed_directory_phone_is_denied(self):
        self.fixture(); self.guard._international_phone=lambda p:'255700000099'
        with patch('approval_gateway.progress.get_workflow_guard', return_value=self.guard):
            with self.assertRaises(ServiceError): handle_progress(self.settings,self.reply)
        self.assertEqual(self.queue_count(),0)

    def test_expired_provider_message_is_not_sent(self):
        self.fixture(); self.reply['timestamp']=str(int(time.time())-86400)
        with patch('approval_gateway.progress.get_workflow_guard', return_value=self.guard), patch('approval_gateway.progress.PetaClient._get', return_value={'success':True,'data':self.data}):
            self.assertEqual(handle_progress(self.settings,self.reply)['reason'],'expired_status_reply')
        self.assertEqual(self.queue_count(),0)

    def test_response_identity_mismatch_is_not_queued(self):
        self.fixture(); self.data['reference_id']='298'
        with patch('approval_gateway.progress.get_workflow_guard', return_value=self.guard), patch('approval_gateway.progress.PetaClient._get', return_value={'success':True,'data':self.data}):
            with self.assertRaises(ServiceError): handle_progress(self.settings,self.reply)
        self.assertEqual(self.queue_count(),0)

    def test_other_conversation_is_untouched(self):
        self.assertIsNone(handle_progress(self.settings,{'text':'Incorrect quantity'}))

    def test_actual_actor_and_current_reviewers_are_separate(self):
        self.fixture(); self.data['latest_action']={'action':'CHECKED','actor':{'username':'adriano'},'position':'HOD','recorded_at':None}
        body=render_progress(self.data)
        self.assertIn('CHECKED by adriano',body)
        self.assertIn('john.john',body)
        self.assertIn('Time not recorded',body)
