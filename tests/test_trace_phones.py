import unittest
from dataclasses import replace
from unittest.mock import patch
import test_recovery as fixture
from approval_gateway import recovery
from approval_gateway.db import connection
from approval_gateway.integrations.permissions import WorkflowDenied, IntegrationUnavailable

class TracePhoneTests(unittest.TestCase):
    setUp=fixture.RecoveryTests.setUp
    peta_response=fixture.RecoveryTests.peta_response
    directory_response=fixture.RecoveryTests.directory_response
    issue=fixture.RecoveryTests.issue
    trigger=fixture.RecoveryTests.trigger
    run_worker=fixture.RecoveryTests.run_worker

    def settings_admin(self):
        return replace(self.settings,whatsapp_admin_phones=('255787550399',))

    def test_trace_shows_saved_names_and_verified_current_phones_without_writes(self):
        tasks=self.issue()
        text=recovery.trace(self.settings_admin(),'255787550399','peta','requisition_approval','9')
        for task in tasks:
            self.assertIn(task['actor_phone'],text)
            self.assertIn(task['actor_name'],text)
        self.assertIn('Directory verified now',text)
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM approval_requests').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0],0)

    def test_directory_unavailable_labels_phone_as_historical(self):
        tasks=self.issue()
        with patch('approval_gateway.recovery.get_workflow_guard',side_effect=IntegrationUnavailable('Unavailable')):
            text=recovery.trace(self.settings_admin(),'255787550399','peta','requisition_approval','9')
        self.assertIn('Current phone not verified',text)
        self.assertIn(tasks[0]['actor_phone'],text)
        self.assertNotIn('Directory verified now',text)

    def test_trace_phone_access_requires_admin(self):
        self.issue()
        with self.assertRaises(WorkflowDenied):
            recovery.trace(replace(self.settings,whatsapp_admin_phones=()),'255787550399','peta','requisition_approval','9')

    def test_changed_verified_phone_does_not_overwrite_saved_task_phone(self):
        tasks=self.issue()
        from approval_gateway.integrations.registry import get_workflow_guard
        guard=get_workflow_guard('peta')
        original=self.directory_response
        def changed(path):
            data=original(path)
            if data['data']['user_details']['id']==152:
                data['data']['user_details']['phone_number']='255700000123'
            return data
        with patch.object(guard.directory,'_get',side_effect=changed):
            text=recovery.trace(self.settings_admin(),'255787550399','peta','requisition_approval','9')
        self.assertIn('Current WhatsApp (Directory verified now): 255700000123',text)
        self.assertIn('Task phone: 255787550399',text)
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT actor_phone FROM approval_requests WHERE id=?',(tasks[0]['id'],)).fetchone()[0],tasks[0]['actor_phone'])
