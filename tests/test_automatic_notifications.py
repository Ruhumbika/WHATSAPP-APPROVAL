import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_gateway as baseline
from approval_gateway.db import connection, utc_now
from approval_gateway.integrations.permissions import WorkflowDenied
from approval_gateway.service import ServiceError
from approval_gateway.whatsapp import WhatsAppClient, WhatsAppError
from approval_gateway.workflow_notifications import (
    context, digest, handle_view, process_automatic_notifications, watch_event,
)


class AutomaticNotificationTests(unittest.TestCase):
    def setUp(self):
        baseline.GatewayTests.setUp(self)
        self.settings = replace(self.settings, workflow_notifications_enabled=True, workflow_reminder_hours=24)
        issued = baseline.GatewayTests.new_request(self)
        with connection(self.settings) as conn:
            conn.execute("""UPDATE approval_requests SET source_reference_id='9',step_id='10',actor_user_id='43',
                actor_directory_uuid='18958e18-4f60-11f1-8fd1-6805cae19a58',delivery_status='simulated',expires_at=? WHERE id=?""",
                ((datetime.now(timezone.utc)+timedelta(days=7)).isoformat(), issued['id']))
        self.creator = {'id': 42, 'username': 'creator', 'email': 'creator@example.test'}
        self.reviewer = {'id': 43, 'username': 'reviewer', 'email': 'reviewer@example.test'}
        self.level = {'id': 10, 'level_id': 1, 'approval_status': 'PENDING',
                      'role': {'name': 'VERIFY'}, 'position': {'role_name': 'Director'},
                      'approvers': [self.reviewer], 'approval': None}
        self.data = {'source_system': 'peta', 'request_type': 'requisition_approval', 'reference_id': '9',
                     'company': '77', 'cycle': '0', 'creator': self.creator, 'completed': False,
                     'deadline': None, 'record': {}, 'progress': {
                         'request_type': 'requisition_approval', 'reference_id': '9', 'status': 'In progress',
                         'record_status': 'PENDING', 'completed_steps': 0, 'total_steps': 1, 'remaining_steps': 1,
                         'current_step': self.level, 'following_step': None, 'latest_action': None,
                         'waiting_since': None, 'levels': [self.level], 'updated_at': utc_now(),
                     }}
        self.numbers = {42: self.phone, 43: '255700000043'}
        self.uuids = {42: self.actor_uuid, 43: '18958e18-4f60-11f1-8fd1-6805cae19a58'}
        directory = SimpleNamespace(resolve_recipient=lambda user: SimpleNamespace(
            peta_user_id=user['id'], phone_number=self.numbers[user['id']],
            directory_user=SimpleNamespace(directory_uuid=self.uuids[user['id']])) )
        self.guard = SimpleNamespace(directory=directory, _international_phone=lambda phone: phone,
            peta=SimpleNamespace(base_url='http://127.0.0.1', document_access_token='x'*40, timeout_seconds=5,
                                 get_document=Mock(return_value=b'%PDF-1.4 request')),
            authorize=lambda request: SimpleNamespace(phone_number=self.numbers[43], source_user_id='43', record_digest='1'))
        self.client = Mock()
        self.client.send_workflow_template.return_value = 'wamid.auto'
        self.client.send_text.return_value = 'wamid.text'
        self.client.send_document.return_value = 'wamid.document'
        self.start_patch('approval_gateway.workflow_notifications.get_workflow_guard', return_value=self.guard)
        self.start_patch('approval_gateway.workflow_notifications.PetaClient._get', side_effect=self.api)
        self.pdf = self.start_patch('approval_gateway.workflow_notifications.completed_pdf', return_value=b'%PDF-1.4 completed')
        self.refresh()
        self.event()

    def start_patch(self, name, **kwargs):
        patcher = patch(name, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def refresh(self):
        self.data['state_key'] = digest([self.data['cycle'], self.data['progress'], self.data['creator']])
        self.data['revision'] = digest([self.data['state_key'], self.data['record']])

    def api(self, path):
        if '/progress/' in path:
            return {'success': True, 'data': copy.deepcopy(self.data['progress'])}
        return {'success': True, 'data': copy.deepcopy(self.data)}

    def event(self, event_id=1):
        with connection(self.settings) as conn:
            watch_event(conn, {'id': event_id, 'source_system': 'peta', 'request_type': self.data['request_type'], 'reference_id': '9'})

    def jobs(self):
        with connection(self.settings) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM workflow_notifications ORDER BY id')]

    def tick(self):
        process_automatic_notifications(self.settings, self.client, 25)

    def age(self, hours):
        with connection(self.settings) as conn:
            conn.execute('UPDATE workflow_watches SET stage_since=?,next_check_at=?',
                         ((datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat(), utc_now()))

    def complete(self):
        self.level['approval_status'] = 'APPROVED'
        self.level['approval'] = {'id': 100, 'actor': {'id': 43, 'username': 'reviewer'}, 'action': 'VERIFIED',
                                  'position': 'Director', 'recorded_at': utc_now(), 'remarks': 'Checked'}
        self.data['completed'] = True
        self.data['progress'].update(status='Fully approved', record_status='APPROVED', completed_steps=1,
            remaining_steps=0, current_step=None, latest_action=self.level['approval'])
        self.refresh()

    def test_duplicate_events_send_one_progress_to_creator(self):
        self.tick(); self.event(2); self.tick()
        self.assertEqual(self.client.send_workflow_template.call_count, 1)
        args = self.client.send_workflow_template.call_args[0]
        self.assertEqual(args[0], self.numbers[42])
        self.assertEqual(args[1], 'workflow_progress_update')
        self.assertEqual(len(args[2]), 7)

    def test_reminders_only_current_reviewers_and_no_catchup_burst(self):
        self.tick(); self.age(49); self.tick(); self.tick()
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.jobs()[-1]['kind'], 'reminder')
        self.assertEqual(self.jobs()[-1]['phone'], self.numbers[43])
        args = self.client.send_workflow_template.call_args[0]
        self.assertEqual(args[1], 'workflow_action_reminder')
        self.assertEqual(args[2][-1], 'No deadline set')
        self.age(73); self.tick(); self.age(120); self.tick()
        self.assertEqual(len([job for job in self.jobs() if job['kind'] == 'reminder']), 2)

    def test_completion_sends_pdf_once_and_stops_reminders(self):
        self.tick(); self.complete(); self.event(2); self.tick(); self.event(3); self.tick()
        self.assertEqual(self.pdf.call_count, 1)
        self.assertEqual([job['kind'] for job in self.jobs()], ['progress', 'retired', 'completed'])
        args = self.client.send_workflow_template.call_args[0]
        self.assertEqual(args[1], 'workflow_completed_document')
        self.assertEqual(len(args[2]), 4)
        self.assertEqual(args[4]['document_media_id'], '1')

    def test_early_approved_record_does_not_send_completed_pdf(self):
        self.data['progress']['record_status'] = 'APPROVED'
        self.refresh(); self.tick()
        self.pdf.assert_not_called()
        self.assertEqual(self.jobs()[0]['kind'], 'progress')

    def test_false_completion_without_recorded_actions_is_rejected(self):
        self.complete(); self.level['approval'] = None; self.refresh(); self.tick()
        self.pdf.assert_not_called()
        self.client.send_workflow_template.assert_not_called()

    def test_changed_directory_phone_is_used_automatically(self):
        self.numbers[42] = '255700000042'
        self.tick()
        self.assertEqual(self.jobs()[0]['phone'], '255700000042')

    def test_disabled_binding_prevents_automatic_send(self):
        with connection(self.settings) as conn:
            conn.execute('UPDATE actor_bindings SET active=0')
        self.tick()
        self.client.send_workflow_template.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'], 'cancelled')

    def test_pending_reminder_is_cancelled_after_completion(self):
        self.tick(); self.age(25)
        self.client.send_workflow_template.side_effect = WhatsAppError('rate limit', http_status=429)
        self.tick()
        self.complete(); self.event(2)
        self.client.send_workflow_template.side_effect = None
        with connection(self.settings) as conn:
            conn.execute("UPDATE workflow_notifications SET next_attempt_at=? WHERE status='pending'", (utc_now(),))
        self.tick()
        reminder = next(job for job in self.jobs() if job['kind'] == 'reminder')
        self.assertEqual(reminder['status'], 'cancelled')

    def test_unknown_acceptance_is_not_retried(self):
        self.client.send_workflow_template.side_effect = WhatsAppError('connection lost', delivery_unknown=True)
        self.tick(); self.event(2); self.tick()
        self.assertEqual(self.client.send_workflow_template.call_count, 1)
        self.assertEqual(self.jobs()[0]['status'], 'unknown')

    def test_correction_and_resubmit_create_new_cycle(self):
        self.tick()
        self.data['cycle'] = '100'
        self.data['progress'].update(status='Awaiting correction', record_status='DRAFT', current_step=None)
        self.refresh(); self.event(2); self.tick()
        self.data['progress'].update(status='In progress', record_status='SUBMITTED', current_step=self.level)
        self.refresh(); self.event(3); self.tick()
        self.assertEqual(len([j for j in self.jobs() if j['kind'] != 'retired']), 3)
        self.pdf.assert_not_called()
        with connection(self.settings) as conn:
            self.assertIsNotNone(conn.execute('SELECT stage_since FROM workflow_watches').fetchone()[0])

    def test_view_progress_authenticates_sender_and_deduplicates(self):
        self.tick(); parent = self.jobs()[0]
        response = {'reference': parent['token'], 'view': 'progress', 'phone': self.phone,
                    'message_id': 'view-1', 'timestamp': str(int(datetime.now(timezone.utc).timestamp()))}
        with self.assertRaises(ServiceError):
            handle_view(self.settings, dict(response, phone='255700000099'))
        handle_view(self.settings, response); handle_view(self.settings, response); self.tick()
        self.assertEqual(self.client.send_text.call_count, 1)
        self.assertIn('Approval journey', self.client.send_text.call_args[0][1])
        self.assertEqual(len(self.jobs()), 2)

    def test_view_request_returns_current_pdf_without_executing_decision(self):
        self.tick(); self.age(25); self.tick(); parent = self.jobs()[-1]
        response = {'reference': parent['token'], 'view': 'request', 'phone': self.numbers[43],
                    'message_id': 'view-request', 'timestamp': str(int(datetime.now(timezone.utc).timestamp()))}
        handle_view(self.settings, response); self.tick()
        self.client.send_document.assert_called_once()
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0], 0)

    def test_payment_advice_completion_uses_payment_document_route_scope(self):
        with connection(self.settings) as conn:
            conn.execute('DELETE FROM workflow_watches')
        self.data['request_type'] = self.data['progress']['request_type'] = 'payment_advice_approval'
        self.complete(); self.event(); self.tick()
        self.assertEqual(self.pdf.call_args[0][1]['request_type'], 'payment_advice_approval')
        self.assertEqual(self.client.send_workflow_template.call_args[0][2][0], 'Payment Advice #9')

    def test_company_change_is_never_followed(self):
        self.tick(); self.data['company'] = '88'; self.event(2); self.tick()
        self.assertEqual(self.client.send_workflow_template.call_count, 1)

    def test_expired_approval_task_is_not_reminded(self):
        self.tick()
        with connection(self.settings) as conn:
            conn.execute("UPDATE approval_requests SET expires_at='2000-01-01T00:00:00+00:00'")
        self.age(25); self.tick()
        self.assertEqual(self.client.send_workflow_template.call_count, 1)
        self.assertEqual(self.jobs()[-1]['status'], 'cancelled')

    def test_meta_template_payloads_match_approved_contracts(self):
        client = WhatsAppClient(replace(self.settings, dry_run_whatsapp=False))
        token = 'WNF-' + 'A'*32
        with patch.object(client, '_post_graph', return_value={'messages': [{'id': 'wamid.test'}]}) as post:
            for name, count in [('workflow_progress_update', 7), ('workflow_action_reminder', 5), ('workflow_completed_document', 4)]:
                client.send_workflow_template(self.phone, name, ['Example']*count, token,
                                              {'document_media_id': '123', 'document_filename': 'completed.pdf'})
                template = post.call_args[0][0]['template']
                from approval_gateway.meta import extract_responses
                button_payload = template['components'][-1]['parameters'][0]['payload']
                replies = list(extract_responses({'object': 'whatsapp_business_account', 'entry': [{'changes': [{
                    'field': 'messages', 'value': {'metadata': {'phone_number_id': '123'}, 'messages': [{
                        'id': 'click-1', 'from': self.phone, 'timestamp': '1', 'type': 'button',
                        'button': {'payload': button_payload}}]}}]}]}, expected_phone_number_id='123'))
                self.assertEqual(replies[0]['action'], 'workflow_view')
                self.assertEqual(replies[0]['reference'], token)
                body = next(c for c in template['components'] if c['type'] == 'body')
                self.assertEqual(len(body['parameters']), count)
                self.assertEqual(template['components'][-1]['parameters'][0]['payload'],
                                 ('workflow_request_' if name == 'workflow_action_reminder' else 'workflow_progress_') + token)


    def test_admin_pause_cancels_pending_and_resume_sends_current_slot(self):
        from approval_gateway import admin_controls as controls
        from approval_gateway.workflow_notifications import sync_watch
        self.settings = replace(self.settings, whatsapp_admin_phones=(self.phone,))
        self.tick()
        self.age(25)
        with connection(self.settings) as conn:
            watch = dict(conn.execute('SELECT * FROM workflow_watches').fetchone())
        sync_watch(self.settings, watch)
        self.assertTrue(any(j['kind'] == 'reminder' and j['status'] == 'pending' for j in self.jobs()))
        controls.apply(self.settings, self.phone, 'pause-1', 'reminders_paused', True, 0)
        self.tick()
        self.assertFalse(any(j['kind'] == 'reminder' and j['status'] in ('pending', 'simulated') for j in self.jobs()))
        controls.apply(self.settings, self.phone, 'resume-1', 'reminders_paused', False, 1)
        self.tick()
        self.assertEqual(sum(j['kind'] == 'reminder' and j['status'] == 'simulated' for j in self.jobs()), 1)

    def test_other_reviewer_completion_closes_draft_and_notifies_once(self):
        self.tick()
        with connection(self.settings) as conn:
            conn.execute("UPDATE approval_requests SET rejection_phase='reason',rejection_draft='Fix price',rejection_review_token='old',rejection_expires_at='2099-01-01T00:00:00+00:00'")
        self.complete()
        self.event(2); self.tick()
        with connection(self.settings) as conn:
            row = conn.execute('SELECT * FROM approval_requests').fetchone()
            self.assertEqual((row['status'], row['rejection_phase'], row['rejection_draft']), ('cancelled', 'closed', None))
        notices = [j for j in self.jobs() if j['kind'] == 'retired']
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['phone'], self.numbers[43])
        args = [c.args for c in self.client.send_workflow_template.call_args_list if 'unfinished rejection' in str(c)]
        self.assertEqual(len(args), 1)
        self.data['progress']['updated_at'] = '2090-01-01T00:00:00+00:00'
        self.refresh(); self.event(3); self.tick()
        self.assertEqual(len([j for j in self.jobs() if j['kind'] == 'retired']), 1)

    def test_required_co_reviewer_is_not_cancelled(self):
        self.tick()
        # Another reviewer action exists, but ERP still requires the same user at this step.
        self.data['progress']['latest_action'] = {'action':'APPROVED','actor':{'id':44,'username':'Other'},'position':'Director'}
        self.refresh(); self.event(2); self.tick()
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT status FROM approval_requests').fetchone()[0], 'pending')
        self.assertFalse(any(j['kind'] == 'retired' for j in self.jobs()))

    def test_poll_reconciles_without_new_event_and_notifications_disabled(self):
        self.tick()
        self.complete()
        self.settings = replace(self.settings, workflow_notifications_enabled=False)
        self.age(1)
        self.tick()
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT status FROM approval_requests').fetchone()[0], 'cancelled')
        self.assertFalse(any(j['kind'] == 'retired' for j in self.jobs()))

    def test_retired_notice_verifies_directory_identity(self):
        self.tick()
        self.complete()
        self.guard.directory.resolve_recipient = lambda user: SimpleNamespace(
            peta_user_id=user['id'], phone_number=self.numbers[user['id']],
            directory_user=SimpleNamespace(directory_uuid='different-identity'))
        self.event(2); self.tick()
        notice = next(j for j in self.jobs() if j['kind'] == 'retired')
        self.assertEqual(notice['status'], 'cancelled')
