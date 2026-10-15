import json
import unittest
from dataclasses import replace
from uuid import uuid4
from unittest.mock import patch
from approval_gateway.db import connection
from approval_gateway import recovery
from approval_gateway.integrations.permissions import WorkflowDenied
from approval_gateway.service import ServiceError
import test_workflow_integration as fixture


class RecoveryTests(unittest.TestCase):
    setUp=fixture.WorkflowIntegrationTests.setUp
    def peta_response(self, path):
        result=fixture.WorkflowIntegrationTests.peta_response(self,path)
        if path.endswith('/approval-chain'):
            levels=result['data']['levels']
            levels[0]['approvers'].extend(levels[1]['approvers'])
        return result
    directory_response=fixture.WorkflowIntegrationTests.directory_response
    trigger=fixture.WorkflowIntegrationTests.trigger
    task=fixture.WorkflowIntegrationTests.task
    run_worker=fixture.WorkflowIntegrationTests.run_worker
    reply=fixture.WorkflowIntegrationTests.reply

    def issue(self):
        self.trigger();self.run_worker()
        with connection(self.settings) as c:
            return [dict(r) for r in c.execute('SELECT * FROM approval_requests ORDER BY id')]

    def expire(self, unknown=False):
        with connection(self.settings) as c:
            c.execute("""UPDATE approval_requests SET status='expired',expires_at='2000-01-01T00:00:00+00:00',
                rejection_phase='reason',rejection_draft='Saved reason',rejection_review_token='old-token'""")
            if unknown:c.execute("UPDATE approval_requests SET delivery_status='unknown'")

    def test_event_renews_expired_tasks_once_and_preserves_history(self):
        old=self.issue();self.expire()
        self.trigger();self.run_worker()
        with connection(self.settings) as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM approval_requests ORDER BY id')]
            self.assertEqual(len(rows),4)
            self.assertEqual([r['status'] for r in rows[:2]],['expired']*2)
            self.assertEqual([r['status'] for r in rows[2:]],['pending']*2)
            self.assertEqual(rows[0]['rejection_draft'],'Saved reason')
            self.assertEqual(rows[0]['rejection_phase'],'closed')
            self.assertIsNone(rows[0]['rejection_review_token'])
            self.assertNotEqual(old[0]['gateway_reference_id'],rows[2]['gateway_reference_id'])
        self.trigger();self.run_worker()
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM approval_requests').fetchone()[0],4)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0],0)

    def test_old_button_remains_invalid_after_renewal(self):
        old=self.issue();self.expire();self.trigger();self.run_worker()
        result=self.reply(old[0])
        self.assertFalse(result['processed'])
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0],0)

    def test_disabled_binding_is_not_reenabled(self):
        self.issue();self.expire()
        with connection(self.settings) as c:c.execute('UPDATE actor_bindings SET active=0')
        self.trigger();self.run_worker()
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM approval_requests').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT SUM(active) FROM actor_bindings').fetchone()[0],0)

    def test_unknown_delivery_is_not_automatically_renewed(self):
        self.issue();self.expire(unknown=True);self.trigger();self.run_worker()
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM approval_requests').fetchone()[0],2)

    def test_callback_evidence_prevents_renewal(self):
        tasks=self.issue();self.reply(tasks[0])
        self.expire()
        self.trigger();self.run_worker()
        with connection(self.settings) as c:
            # Event is atomic: a callback conflict does not partially renew reviewers.
            self.assertEqual(c.execute('SELECT COUNT(*) FROM approval_requests').fetchone()[0],2)

    def test_inbox_recovers_only_verified_actor(self):
        old=self.issue();self.expire()
        from approval_gateway.integrations.registry import get_workflow_guard
        g=get_workflow_guard('peta')
        with connection(self.settings) as c:
            binding=dict(c.execute('SELECT * FROM actor_bindings WHERE directory_uuid=?',(old[0]['actor_directory_uuid'],)).fetchone())
        actor=g.authorize({'request_type':'requisition_approval','reference_id':'9','company':'77',
                           'step_id':old[0]['step_id'],'actor_directory_uuid':old[0]['actor_directory_uuid']})
        fresh=recovery.renew_for_actor(self.settings,'peta','requisition_approval','9',binding,actor)
        self.assertEqual(fresh['actor_directory_uuid'],old[0]['actor_directory_uuid'])
        self.assertNotEqual(fresh['id'],old[0]['id'])
        again=recovery.renew_for_actor(self.settings,'peta','requisition_approval','9',binding,actor)
        self.assertEqual(fresh['id'],again['id'])
        with connection(self.settings) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM approval_requests').fetchone()[0],3)

    def test_admin_reconcile_is_durable_idempotent_and_requires_current_allowlist(self):
        self.issue()
        settings=replace(self.settings,whatsapp_admin_phones=('255787550399',))
        for _ in range(2):recovery.reconcile(settings,'255787550399','admin-click','peta','requisition_approval','9')
        with connection(settings) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM workflow_events WHERE status='pending'").fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM gateway_admin_audit').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0],0)
        with self.assertRaises(WorkflowDenied):
            recovery.reconcile(replace(self.settings,whatsapp_admin_phones=()),'255787550399','another','peta','requisition_approval','9')

    def test_trace_is_read_only_and_reports_current_erp(self):
        self.issue();self.expire()
        settings=replace(self.settings,whatsapp_admin_phones=('255787550399',))
        text=recovery.trace(settings,'255787550399','peta','requisition_approval','9')
        self.assertIn('expired',text);self.assertIn('ERP verified current assignments:',text)
        with connection(settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM approval_requests').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM workflow_events').fetchone()[0],1)

    def test_retry_unknown_denied_and_confirmed_4xx_requires_live_eligibility(self):
        old=self.issue()
        settings=replace(self.settings,whatsapp_admin_phones=('255787550399',))
        with self.assertRaises(WorkflowDenied):recovery.retry_delivery(settings,'255787550399','x',old[0]['id'])
        with connection(settings) as c:
            c.execute("""UPDATE approval_requests SET delivery_status='failed',whatsapp_message_id=NULL,
                delivery_error='WhatsApp request failed: HTTP 400, API code 131000.' WHERE id=?""",(old[0]['id'],))
        result=recovery.retry_delivery(settings,'255787550399','retry-1',old[0]['id'])
        self.assertIn('queued',result)
        with connection(settings) as c:
            row=c.execute('SELECT * FROM approval_requests WHERE id=?',(old[0]['id'],)).fetchone()
            self.assertEqual(row['delivery_status'],'pending')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM gateway_admin_audit').fetchone()[0],1)

    def test_watch_schedules_once_for_expired_current_reviewers(self):
        old=self.issue();self.expire()
        watch={'source_system':'peta','request_type':'requisition_approval','reference_id':'9'}
        data={'company':'77','progress':{'current_step':{'id':old[0]['step_id'],
              'approvers':[{'id':int(t['actor_user_id'])} for t in old]}}}
        with connection(self.settings) as c:
            recovery.schedule_expired(c,watch,data);recovery.schedule_expired(c,watch,data)
        self.run_worker()
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM workflow_events').fetchone()[0],2)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM approval_requests WHERE status='pending'").fetchone()[0],2)

    def test_inbox_document_renews_and_satisfies_delivery_without_duplicate_send(self):
        from unittest.mock import Mock
        from approval_gateway.inbox import enqueue_reply, process_inbox
        from approval_gateway.integrations.registry import get_workflow_guard
        import time
        old=self.issue();self.expire()
        with connection(self.settings) as c:
            binding=dict(c.execute('SELECT * FROM actor_bindings WHERE directory_uuid=?',(old[0]['actor_directory_uuid'],)).fetchone())
        guard=get_workflow_guard('peta')
        command={'kind':'requisitions','reference':'9','company':'77','cursor':0}
        response={'phone':old[0]['actor_phone'],'message_id':'recover-inbox','timestamp':str(int(time.time()))}
        enqueue_reply(self.settings,response,command)
        item={'company':'77','kind':'requisitions','reference_id':'9','can_act':True,
              'current_step':{'id':old[0]['step_id']},'purpose':'Parts','amounts':{},'status':'PENDING','completed_steps':0,'total_steps':2}
        client=Mock()
        client.send_inbox_document.return_value='wamid.recovered'
        with patch('approval_gateway.inbox.identity',return_value=(binding,guard)), \
             patch('approval_gateway.inbox.read_api',return_value=item), \
             patch('approval_gateway.service_context.discover',return_value=([{'system':'peta','id':'4','name':'PETA'}],False)), \
             patch.object(self.peta,'get_document',return_value=b'%PDF-1.4 test'):
            process_inbox(self.settings,client,5)
        client.send_inbox_document.assert_called_once()
        with connection(self.settings) as c:
            task=dict(c.execute('SELECT * FROM approval_requests ORDER BY id DESC LIMIT 1').fetchone())
            self.assertEqual(task['delivery_status'],'simulated')
            self.assertEqual(task['whatsapp_message_id'],'wamid.recovered')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM approval_requests').fetchone()[0],3)
        result,transport=self.run_worker()
        transport.return_value.send_approval_request.assert_not_called()

    def test_concurrent_inbox_recovery_creates_only_one_replacement(self):
        from concurrent.futures import ThreadPoolExecutor
        from approval_gateway.integrations.registry import get_workflow_guard
        old=self.issue();self.expire()
        with connection(self.settings) as c:
            binding=dict(c.execute('SELECT * FROM actor_bindings WHERE directory_uuid=?',(old[0]['actor_directory_uuid'],)).fetchone())
        guard=get_workflow_guard('peta')
        actor=guard.authorize({'request_type':'requisition_approval','reference_id':'9','company':'77',
                              'step_id':old[0]['step_id'],'actor_directory_uuid':old[0]['actor_directory_uuid']})
        def renew(_):
            return recovery.renew_for_actor(self.settings,'peta','requisition_approval','9',binding,actor)['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(renew,range(2)))
        self.assertEqual(result[0],result[1])
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM approval_requests').fetchone()[0],3)
