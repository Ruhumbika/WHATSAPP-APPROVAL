import json
import unittest
from dataclasses import replace
from uuid import uuid4
from unittest.mock import Mock, patch
import test_recovery as fixture
from approval_gateway.db import connection
from approval_gateway import admin_controls, recovery
from approval_gateway.service import process_workflow_event
from approval_gateway.worker import _claim_delivery, _send_delivery


class PauseRecoveryTests(unittest.TestCase):
    peta_response=fixture.RecoveryTests.peta_response
    directory_response=fixture.RecoveryTests.directory_response
    issue=fixture.RecoveryTests.issue
    expire=fixture.RecoveryTests.expire
    trigger=fixture.RecoveryTests.trigger
    run_worker=fixture.RecoveryTests.run_worker

    def setUp(self):
        fixture.RecoveryTests.setUp(self)
        self.settings=replace(self.settings,whatsapp_admin_phones=('255787550399',))

    def pause(self, enabled=True):
        revision,_=admin_controls.read(self.settings)
        admin_controls.apply(self.settings,'255787550399',str(uuid4()),
                             'reminders_paused',enabled,revision)

    def watch_data(self, tasks):
        return ({'source_system':'peta','request_type':'requisition_approval','reference_id':'9'},
                {'company':'77','progress':{'current_step':{'id':tasks[0]['step_id'],
                 'approvers':[{'id':int(t['actor_user_id'])} for t in tasks]}}})

    def claim_event(self):
        self.trigger()
        with connection(self.settings) as c:
            row=c.execute("SELECT id FROM workflow_events WHERE status='pending' ORDER BY id DESC LIMIT 1").fetchone()
            c.execute("UPDATE workflow_events SET status='processing',attempts=attempts+1 WHERE id=?",(row['id'],))
        process_workflow_event(self.settings,row['id'])

    def test_pause_prevents_scheduling_and_resume_can_schedule(self):
        tasks=self.issue();self.expire();self.pause()
        watch,data=self.watch_data(tasks)
        with connection(self.settings) as c:
            recovery.schedule_expired(c,watch,data)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM workflow_events').fetchone()[0],1)
        self.pause(False)
        with connection(self.settings) as c:
            recovery.schedule_expired(c,watch,data)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM workflow_events').fetchone()[0],2)

    def test_queued_recovery_event_cannot_send_after_pause(self):
        tasks=self.issue();self.expire()
        watch,data=self.watch_data(tasks)
        with connection(self.settings) as c:recovery.schedule_expired(c,watch,data)
        self.pause()
        _,client=self.run_worker()
        client.return_value.send_approval_request.assert_not_called()
        with connection(self.settings) as c:
            rows=c.execute("SELECT * FROM approval_requests WHERE status='pending'").fetchall()
            self.assertEqual(len(rows),2)
            self.assertTrue(all(r['delivery_status']=='paused' for r in rows))
            self.assertTrue(all(r['delivery_attempts']==0 for r in rows))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0],0)

    def test_already_created_recovery_tasks_are_parked_and_resume(self):
        self.issue();self.expire();self.claim_event();self.pause()
        self.run_worker()
        self.pause(False)
        _,client=self.run_worker()
        self.assertEqual(client.return_value.send_approval_request.call_count,2)
        with connection(self.settings) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM approval_requests WHERE status='pending' AND delivery_status='simulated'").fetchone()[0],2)

    def test_new_stage_initial_approvals_are_not_suppressed(self):
        self.pause()
        self.trigger()
        _,client=self.run_worker()
        self.assertEqual(client.return_value.send_approval_request.call_count,2)

    def test_explicit_admin_reconciliation_is_allowed_while_paused(self):
        self.issue();self.expire();self.pause()
        recovery.reconcile(self.settings,'255787550399','manual-reconcile','peta','requisition_approval','9')
        _,client=self.run_worker()
        self.assertEqual(client.return_value.send_approval_request.call_count,2)

    def test_user_requested_recovery_is_allowed_while_paused(self):
        tasks=self.issue();self.expire();self.pause()
        from approval_gateway.integrations.registry import get_workflow_guard
        with connection(self.settings) as c:
            binding=dict(c.execute('SELECT * FROM actor_bindings WHERE directory_uuid=?',(tasks[0]['actor_directory_uuid'],)).fetchone())
        guard=get_workflow_guard('peta')
        actor=guard.authorize({'request_type':'requisition_approval','reference_id':'9',
            'company':'77','step_id':tasks[0]['step_id'],'actor_directory_uuid':tasks[0]['actor_directory_uuid']})
        recovery.renew_for_actor(self.settings,'peta','requisition_approval','9',binding,actor)
        _,client=self.run_worker()
        self.assertEqual(client.return_value.send_approval_request.call_count,1)

    def test_legacy_renewal_queue_is_recognized_without_new_origin_metadata(self):
        self.issue();self.expire();self.claim_event()
        with connection(self.settings) as c:
            c.execute("UPDATE audit_log SET details_json=json_remove(details_json,'$.renewal_origin','$.renewal_parent') WHERE event='created'")
        self.pause()
        _,client=self.run_worker()
        client.return_value.send_approval_request.assert_not_called()
        with connection(self.settings) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM approval_requests WHERE delivery_status='paused'").fetchone()[0],2)

    def test_pause_during_document_preparation_is_rechecked(self):
        self.issue();self.expire();self.claim_event()
        claimed=_claim_delivery(self.settings)
        def prepare(settings, request_id):
            self.pause()
            return claimed
        client=Mock()
        with patch('approval_gateway.worker._prepare_delivery',side_effect=prepare), \
             patch('approval_gateway.worker.get_workflow_guard',return_value=Mock(prepare_delivery=Mock(return_value={}))), \
             patch('approval_gateway.worker.verify_delivery_workflow'):
            _send_delivery(self.settings,client,claimed)
        client.send_approval_request.assert_not_called()

    def test_resume_never_revives_unknown_acceptance(self):
        self.issue();self.expire();self.claim_event()
        with connection(self.settings) as c:
            c.execute("UPDATE approval_requests SET delivery_status='unknown' WHERE status='pending'")
        self.pause();self.pause(False)
        _,client=self.run_worker()
        client.return_value.send_approval_request.assert_not_called()
        with connection(self.settings) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM approval_requests WHERE delivery_status='unknown'").fetchone()[0],2)

class StandardReminderPauseTests(unittest.TestCase):
    def test_standard_reminder_pause_is_rechecked_after_preparation(self):
        import test_automatic_notifications as notifications
        from approval_gateway.workflow_notifications import sync_watch, send_job
        x=notifications.AutomaticNotificationTests()
        x.setUp()
        self.addCleanup(x.doCleanups)
        x.tick()
        x.age(25)
        with connection(x.settings) as c:
            watch=dict(c.execute('SELECT * FROM workflow_watches').fetchone())
        sync_watch(x.settings,watch)
        with connection(x.settings) as c:
            job=dict(c.execute("SELECT * FROM workflow_notifications WHERE kind='reminder' ORDER BY id DESC LIMIT 1").fetchone())
        x.client.reset_mock()
        def prepare(settings, job, watch):
            with connection(settings) as c:
                c.execute('UPDATE gateway_controls SET values_json=? WHERE id=1',
                          (json.dumps({'reminders_paused':True}),))
            return '255787550399','workflow_action_reminder',['value']*5,None
        with patch('approval_gateway.workflow_notifications.prepare_send',side_effect=prepare):
            send_job(x.settings,x.client,job)
        x.client.send_workflow_template.assert_not_called()
        with connection(x.settings) as c:
            row=c.execute('SELECT status,last_error FROM workflow_notifications WHERE id=?',(job['id'],)).fetchone()
            self.assertEqual(row['status'],'cancelled')
            self.assertIn('paused',row['last_error'])
