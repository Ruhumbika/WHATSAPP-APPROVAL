import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import test_gateway as baseline
from approval_gateway.db import connection
from approval_gateway.inbox import handle_inbox, parse, process_inbox
from approval_gateway.integrations.permissions import WorkflowActor, WorkflowDenied, IntegrationUnavailable
from approval_gateway.integrations.registry import configure_workflow_guards
from approval_gateway.service import ServiceError
from approval_gateway.whatsapp import WhatsAppClient, WhatsAppError
from approval_gateway.meta import extract_responses

class InboxTests(unittest.TestCase):
    def setUp(self):
        baseline.GatewayTests.setUp(self)
        self.task = baseline.GatewayTests.new_request(self)
        with connection(self.settings) as conn:
            conn.execute("UPDATE approval_requests SET source_reference_id='9',step_id='10',delivery_status='simulated' WHERE id=?", (self.task['id'],))
        self.actor = WorkflowActor(self.actor_uuid, '42', self.phone, '3', '10', '1')
        from approval_gateway.service import _workflow_evidence
        with connection(self.settings) as conn:
            row = conn.execute('SELECT payload_json FROM approval_requests WHERE id=?', (self.task['id'],)).fetchone()
            payload = json.loads(row[0])
            payload['_gateway_workflow'] = _workflow_evidence(self.actor)
            conn.execute('UPDATE approval_requests SET payload_json=? WHERE id=?', (json.dumps(payload), self.task['id']))
        self.directory_phone = self.phone
        self.guard = SimpleNamespace(
            directory=SimpleNamespace(get_service_details=lambda **kwargs: (object(), {'phone_number': self.directory_phone}), source_user_id=lambda user: 42),
            _international_phone=lambda p: p,
            peta=SimpleNamespace(base_url='http://127.0.0.1', document_access_token='x'*40, timeout_seconds=3,
                                 get_document=Mock(return_value=b'%PDF-1.4 test')),
            authorize=Mock(side_effect=lambda request: self.actor))
        configure_workflow_guards({'peta': self.guard})
        self.item = {'company': '77', 'company_name': 'PETA', 'kind': 'requisitions', 'reference_id': '9',
                     'can_act': True, 'status': 'In progress', 'purpose': 'Office equipment',
                     'requester': 'Creator', 'amounts': {'TZS': '450,000.00'}, 'completed_steps': 0, 'total_steps': 6,
                     'current_step': {'id': '10', 'action': 'CHECK', 'position': 'Director'}}
        self.page = {'company': '77', 'actor_user_id': '42', 'items': [self.item], 'next_cursor': 5}
        p = patch('approval_gateway.inbox.PetaClient._get', side_effect=lambda path: {'success': True, 'data': copy.deepcopy(self.item if '/inbox/' in path else self.page)})
        p.start(); self.addCleanup(p.stop)
        self.client = Mock()
        self.client.send_text.return_value = 'wamid.inbox'
        self.client.send_menu.return_value = 'wamid.menu'
        self.client.send_inbox_document.return_value = 'wamid.document'
        self.reply = {'action': 'conversation', 'text': 'PENDING', 'phone': self.phone, 'message_id': 'inbox-1', 'timestamp': str(int(time.time()))}

    def jobs(self):
        with connection(self.settings) as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM inbox_replies ORDER BY id')]

    def run_command(self, text='PENDING'):
        self.reply['text'] = text
        handle_inbox(self.settings, self.reply)
        process_inbox(self.settings, self.client, 5)
        return self.jobs()[-1]

    def button(self, **kwargs):
        result = dict(self.reply, action='inbox_decision', decision='approved', reference=self.jobs()[-1]['token'],
                      context_id='wamid.document', message_id='button-1', text='approve')
        result.update(kwargs)
        return result

    def test_commands_do_not_take_rejection_reasons(self):
        for text in ('1', 'Incorrect quantity', 'REQ 0'): self.assertIsNone(parse(text))
        self.assertEqual(parse('req-0009 C77')['reference'], '9')
        self.assertEqual(parse('PENDING C77 NEXT 5')['cursor'], 5)
        self.assertEqual(parse('PA 108')['kind'], 'payment-advices')

    def test_pending_durable_dedup_and_pagination(self):
        handle_inbox(self.settings, self.reply)
        self.run_command()
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]['status'], 'simulated')
        body = self.client.send_menu.call_args.args[1]
        self.assertIn('REQ 9 C77', body)
        self.assertIn('PENDING C77 NEXT 5', body)
        process_inbox(self.settings, self.client, 5)
        self.client.send_menu.assert_called_once()

    def test_expired_inbound_is_not_queued(self):
        self.reply['timestamp'] = str(int(time.time()) - 86400)
        self.assertEqual(handle_inbox(self.settings, self.reply)['reason'], 'expired_inbox_reply')
        self.assertFalse(self.jobs())

    def test_future_timestamp_is_rejected(self):
        self.reply['timestamp'] = str(int(time.time()) + 600)
        with self.assertRaises(ServiceError): handle_inbox(self.settings, self.reply)

    def test_changed_directory_phone_is_authoritative(self):
        self.directory_phone = '255700000099'
        self.reply['phone'] = self.directory_phone
        self.run_command()
        self.assertIn('REQ 9', self.client.send_menu.call_args.args[1])

    def test_old_phone_cannot_read_after_directory_change(self):
        self.directory_phone = '255700000099'
        self.run_command()
        self.assertNotIn('Office equipment', self.client.send_text.call_args.args[1])

    def test_disabled_binding_has_no_data(self):
        with connection(self.settings) as conn: conn.execute('UPDATE actor_bindings SET active=0')
        self.run_command()
        self.assertIn('No active PETA identity', self.client.send_text.call_args.args[1])

    def test_scope_mismatch_does_not_send_data(self):
        self.page['company'] = '99'
        self.run_command()
        self.assertNotIn('Office equipment', self.client.send_text.call_args.args[1])

    def test_unavailable_erp_retries_without_unverified_list(self):
        with patch('approval_gateway.inbox.read_api', side_effect=IntegrationUnavailable()): job = self.run_command()
        self.assertEqual(job['status'], 'pending')
        self.client.send_text.assert_not_called()

    def test_search_sends_pdf_with_same_task_action(self):
        job = self.run_command('REQ 9')
        self.assertEqual(job['task_id'], self.task['id'])
        self.assertEqual(job['status'], 'simulated')
        self.client.send_inbox_document.assert_called_once()
        self.assertTrue(handle_inbox(self.settings, self.button())['processed'])
        self.assertFalse(handle_inbox(self.settings, self.button())['processed'])
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT status FROM approval_requests').fetchone()[0], 'approved')

    def test_reject_starts_existing_reason_flow(self):
        self.run_command('REQ 9')
        handle_inbox(self.settings, self.button(decision='rejected', text='reject'))
        with connection(self.settings) as conn: row = conn.execute('SELECT status,rejection_phase FROM approval_requests').fetchone()
        self.assertEqual(tuple(row), ('pending', 'choice'))

    def test_forged_context_or_sender_cannot_decide(self):
        self.run_command('REQ 9')
        for changes in ({'context_id': 'wrong'}, {'phone': '255700000099'}, {'reference': 'IBX-'+'0'*32}):
            with self.assertRaises(ServiceError): handle_inbox(self.settings, self.button(**changes))

    def test_changed_assignment_blocks_action(self):
        self.run_command('REQ 9')
        self.guard.authorize.side_effect = WorkflowDenied('Reassigned')
        with self.assertRaises(ServiceError): handle_inbox(self.settings, self.button())

    def test_expired_task_has_summary_without_buttons(self):
        with connection(self.settings) as conn: conn.execute("UPDATE approval_requests SET expires_at='2000-01-01T00:00:00+00:00'")
        self.run_command('REQ 9')
        self.client.send_inbox_document.assert_not_called()
        self.assertIn('Review this request in PETA', self.client.send_text.call_args.args[1])

    def test_changed_snapshot_has_no_buttons(self):
        self.actor = WorkflowActor(self.actor_uuid, '42', self.phone, '3', '10', 'changed')
        self.run_command('REQ 9')
        self.client.send_inbox_document.assert_not_called()

    def test_creator_or_previous_reviewer_gets_summary_only(self):
        self.item['can_act'] = False
        self.run_command('REQ 9')
        self.client.send_inbox_document.assert_not_called()
        self.assertIn('No action is currently assigned', self.client.send_text.call_args.args[1])

    def test_ambiguous_acceptance_is_not_retried(self):
        self.client.send_menu.side_effect = WhatsAppError('Timeout', delivery_unknown=True)
        self.assertEqual(self.run_command()['status'], 'unknown')
        process_inbox(self.settings, self.client, 5)
        self.client.send_menu.assert_called_once()

    def test_empty_page_is_clear(self):
        self.page.update(items=[], next_cursor=None)
        self.run_command()
        self.assertIn('No pending approvals found', self.client.send_menu.call_args.args[1])

    def test_provider_button_payload_roundtrip(self):
        from dataclasses import replace
        client = WhatsAppClient(replace(self.settings, dry_run_whatsapp=False))
        token = 'IBX-'+'A'*32
        with patch.object(client, '_post_graph', return_value={'messages': [{'id': 'wamid.x'}]}) as post:
            client.send_inbox_document(self.phone, 'Review', {'document_media_id': '123', 'document_filename': 'REQ-9.pdf'}, token)
        interactive = post.call_args.args[0]['interactive']
        self.assertEqual(interactive['header']['type'], 'document')
        for button in interactive['action']['buttons']:
            payload = {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'field': 'messages', 'value': {
                'metadata': {'phone_number_id': '123'}, 'messages': [{'id': 'wamid.in', 'from': self.phone,
                'timestamp': str(int(time.time())), 'context': {'id': 'wamid.x'}, 'type': 'interactive',
                'interactive': {'type': 'button_reply', 'button_reply': button['reply']}}]}}]}]}
            response = list(extract_responses(payload, expected_phone_number_id='123'))[0]
            self.assertEqual(response['action'], 'inbox_decision')
            self.assertEqual(response['reference'], token)

    def test_multiple_companies_require_explicit_scope(self):
        with connection(self.settings) as conn:
            conn.execute("INSERT INTO actor_bindings(system_name,company,directory_uuid,source_user_id,approver_id,active) SELECT system_name,'88',directory_uuid,source_user_id,approver_id,active FROM actor_bindings LIMIT 1")
        self.run_command()
        self.assertIn('Select a company', self.client.send_text.call_args.args[1])
        self.reply['message_id'] = 'inbox-2'
        self.run_command('PENDING C77')
        self.assertIn('REQ 9', self.client.send_menu.call_args.args[1])

    def test_phone_change_during_document_preparation_prevents_send(self):
        def download(*args):
            self.directory_phone = '255700000099'
            return b'%PDF-1.4'
        self.guard.peta.get_document.side_effect = download
        self.run_command('REQ 9')
        self.client.send_inbox_document.assert_not_called()

    def test_queue_expiry_prevents_late_freeform_send(self):
        handle_inbox(self.settings, self.reply)
        with connection(self.settings) as conn: conn.execute("UPDATE inbox_replies SET expires_at='2000-01-01T00:00:00+00:00'")
        process_inbox(self.settings, self.client, 5)
        self.assertEqual(self.jobs()[0]['status'], 'expired')
        self.client.send_text.assert_not_called()

    def test_payment_advice_search_uses_its_own_guard_type(self):
        self.item['kind'] = 'payment-advices'
        self.item['can_act'] = False
        self.run_command('PA 9')
        self.assertIn('PA 9', self.client.send_text.call_args.args[1])

    def test_too_many_page_items_are_rejected(self):
        self.page['items'] = [self.item] * 6
        job = self.run_command()
        self.assertEqual(job['status'], 'pending')
        self.client.send_text.assert_not_called()

    def test_rejection_pdf_view_is_read_only_and_preserves_draft(self):
        from approval_gateway.chat_menu import handle
        from approval_gateway.service import decide
        decide(self.settings, dict(self.reply, action='rejected', reference=self.task['gateway_reference_id']))
        decide(self.settings, dict(self.reply, action='conversation', text='1', message_id='choice',
                                   reference=self.task['gateway_reference_id']))
        decide(self.settings, dict(self.reply, action='conversation', text='Wrong price', message_id='reason',
                                   reference=self.task['gateway_reference_id']))
        handle(self.settings, dict(self.reply, text='HEY', message_id='hey'))
        process_inbox(self.settings, self.client, 5)
        parent = self.jobs()[-1]
        options = json.loads(parent['actions_json'])
        index = next(i for i,o in enumerate(options) if o['title'] == 'View request')
        handle(self.settings, dict(self.reply, action='menu', reference=parent['token'], context_id='wamid.menu',
                                   message_id='view', menu_index=index))
        self.client.send_document.return_value = 'wamid.readonly'
        process_inbox(self.settings, self.client, 5)
        self.client.send_document.assert_called_once()
        self.client.send_inbox_document.assert_not_called()
        with connection(self.settings) as conn:
            row = conn.execute('SELECT status,rejection_draft FROM approval_requests').fetchone()
            self.assertEqual(tuple(row), ('pending', 'Wrong price'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0], 0)

    def test_list_selection_shows_summary_before_pdf(self):
        from approval_gateway.chat_menu import handle
        self.run_command()
        parent = self.jobs()[-1]
        options = json.loads(parent['actions_json'])
        index = next(i for i,o in enumerate(options) if o['title'] == 'REQ 9')
        handle(self.settings, dict(self.reply, action='menu', reference=parent['token'],
              context_id='wamid.menu', message_id='select-summary', menu_index=index))
        process_inbox(self.settings, self.client, 5)
        self.client.send_inbox_document.assert_not_called()
        self.assertIn('View document', self.jobs()[-1]['actions_json'])
        self.assertIn('Office equipment', self.client.send_menu.call_args.args[1])

    def test_typing_failure_does_not_block_reply(self):
        self.client.send_typing.side_effect = RuntimeError('Transient typing failure')
        self.assertEqual(self.run_command()['status'], 'simulated')
