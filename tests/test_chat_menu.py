import json
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
import test_gateway as baseline
from approval_gateway import admin_controls as controls, chat_menu as menu
from approval_gateway.config import _admin_phones, ConfigurationError
from approval_gateway.db import connection
from approval_gateway.inbox import process_inbox
from approval_gateway.integrations.permissions import WorkflowDenied
from approval_gateway.meta import extract_responses
from approval_gateway.service import ServiceError, decide
from approval_gateway.whatsapp import WhatsAppClient


class ChatMenuTests(unittest.TestCase):
    def setUp(self):
        baseline.GatewayTests.setUp(self)
        self.settings = replace(self.settings, whatsapp_admin_phones=(self.phone,))
        self.client = Mock()
        self.client.send_menu.side_effect = lambda *a: 'wamid.' + a[-1]
        self.client.send_text.return_value = 'wamid.text'
        self.counter = 0

    def response(self, text='MENU', **changes):
        self.counter += 1
        result = dict(action='conversation', text=text, phone=self.phone,
                      message_id=f'menu-in-{self.counter}', timestamp=str(int(time.time())))
        result.update(changes)
        return result

    def jobs(self):
        with connection(self.settings) as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM inbox_replies ORDER BY id')]

    def send(self, text='MENU', **changes):
        result = menu.handle(self.settings, self.response(text, **changes))
        process_inbox(self.settings, self.client, 25)
        return result

    def click(self, title, process=True):
        parent = self.jobs()[-1]
        options = json.loads(parent['actions_json'])
        index = next(i for i, o in enumerate(options) if o['title'] == title)
        response = self.response(action='menu', reference=parent['token'],
                                 context_id=parent['message_id'], menu_index=index)
        result = menu.handle(self.settings, response)
        if process:
            process_inbox(self.settings, self.client, 25)
        return result

    def begin_rejection(self):
        task = baseline.GatewayTests.new_request(self)
        decide(self.settings, self.response(action='rejected', reference=task['gateway_reference_id']))
        decide(self.settings, self.response('1', reference=task['gateway_reference_id']))
        return task

    def test_unknown_text_returns_main_without_decision(self):
        self.send('What can this assistant do?')
        options = json.loads(self.jobs()[-1]['actions_json'])
        self.assertEqual([o['title'] for o in options], ['Pending approvals', 'Find a request', 'User guide', 'Contact support', 'Admin controls'])

    def test_help_topics_and_find_validation(self):
        self.send('HELP')
        self.click('Find a request')
        self.assertIn('REQ 312', self.client.send_menu.call_args.args[1])
        self.send('MENU')
        self.click('Find a request')
        self.send('please find it')
        self.assertIn('not recognized', self.client.send_menu.call_args.args[1])
        self.assertIsNone(menu.handle(self.settings, self.response('REQ 312')))

    def test_public_more_has_no_admin_controls(self):
        self.settings = replace(self.settings, whatsapp_admin_phones=())
        self.send()
        self.assertNotIn('Admin controls', self.jobs()[-1]['actions_json'])
        self.send('ADMIN')
        self.assertIn('not enabled', self.client.send_menu.call_args.args[1])

    def test_removed_admin_cannot_click_old_confirmation(self):
        self.send('ADMIN')
        self.click('Reminder settings')
        self.click('Pause reminders')
        self.settings = replace(self.settings, whatsapp_admin_phones=())
        self.click('Confirm change')
        self.assertFalse(controls.read(self.settings)[1]['reminders_paused'])
        self.assertIn('not enabled', self.client.send_menu.call_args.args[1])

    def test_removed_admin_cannot_execute_queued_action(self):
        self.send('ADMIN')
        self.click('Reminder settings')
        self.click('Pause reminders')
        self.click('Confirm change', process=False)
        self.settings = replace(self.settings, whatsapp_admin_phones=())
        process_inbox(self.settings, self.client, 25)
        self.assertFalse(controls.read(self.settings)[1]['reminders_paused'])

    def test_confirm_required_and_audited(self):
        self.send('ADMIN')
        self.click('Reminder settings')
        self.click('Pause reminders')
        self.assertFalse(controls.read(self.settings)[1]['reminders_paused'])
        self.click('Confirm change')
        self.assertTrue(controls.read(self.settings)[1]['reminders_paused'])
        self.assertEqual(controls.effective(self.settings).workflow_reminder_limit, 0)
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM gateway_admin_audit').fetchone()[0], 1)

    def test_content_change_requires_review(self):
        self.send('ADMIN')
        self.click('Help content')
        self.click('Support details')
        self.send('Contact our accounts team.')
        self.assertNotEqual(controls.read(self.settings)[1]['support'], 'Contact our accounts team.')
        self.click('Confirm change')
        self.assertEqual(controls.read(self.settings)[1]['support'], 'Contact our accounts team.')

    def test_help_preserves_rejection_and_context_reply_submits_reason(self):
        task = self.begin_rejection()
        self.send('HELP')
        parent = self.jobs()[-1]
        with connection(self.settings) as conn:
            self.assertEqual(tuple(conn.execute('SELECT status,rejection_phase FROM approval_requests').fetchone()), ('pending', 'reason'))
        response = self.response('Wrong quantity supplied', context_id=parent['message_id'])
        self.assertIsNone(menu.handle(self.settings, response))
        self.assertEqual(response['reference'], task['gateway_reference_id'])
        decide(self.settings, response)
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT status FROM approval_requests').fetchone()[0], 'pending')
            self.assertEqual(conn.execute('SELECT rejection_draft FROM approval_requests').fetchone()[0], 'Wrong quantity supplied')

    def test_menu_during_rejection_requires_explicit_exit(self):
        self.begin_rejection()
        self.send('MENU')
        self.assertIn('unfinished rejection is saved', self.client.send_menu.call_args.args[1])
        self.click('Resume rejection')
        self.click('Cancel rejection')
        with connection(self.settings) as conn:
            row = conn.execute('SELECT status,rejection_phase FROM approval_requests').fetchone()
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['rejection_phase'], 'closed')

    def test_forged_menu_sender_context_or_index_denied(self):
        self.send()
        parent = self.jobs()[-1]
        for change in ({'phone': '255700000000'}, {'context_id': 'wrong'}, {'menu_index': 9}):
            response = self.response(action='menu', reference=parent['token'], context_id=parent['message_id'], menu_index=0)
            response.update(change)
            menu.handle(self.settings, response)
            self.assertEqual(json.loads(self.jobs()[-1]['command_json'])['ui'], 'recovery')

    def test_menu_queue_dedup_expiry_and_rate_limit(self):
        response = self.response()
        menu.handle(self.settings, response)
        self.assertFalse(menu.handle(self.settings, response)['processed'])
        old = self.response(timestamp=str(int(time.time())-86400))
        self.assertFalse(menu.handle(self.settings, old)['processed'])
        for _ in range(9):
            menu.handle(self.settings, self.response())
        self.assertEqual(menu.handle(self.settings, self.response())['reason'], 'inbox_rate_limited')
        self.assertEqual(len(self.jobs()), 10)

    def test_admin_cannot_skip_approval_authorization(self):
        self.send('ADMIN')
        self.assertIsNone(menu.handle(self.settings, self.response(action='approved', reference='AGR-FAKE')))
        with self.assertRaises(ServiceError):
            decide(self.settings, self.response(action='approved', reference='AGR-FAKE'))

    def test_stale_revision_and_replay_do_not_overwrite_settings(self):
        controls.apply(self.settings, self.phone, 'first', 'reminder_hours', 12, 0)
        controls.apply(self.settings, self.phone, 'first', 'reminder_hours', 12, 0)
        with self.assertRaises(WorkflowDenied):
            controls.apply(self.settings, self.phone, 'second', 'reminder_hours', 4, 0)
        self.assertEqual(controls.read(self.settings)[1]['reminder_hours'], 12)

    def test_allowlist_is_strict_and_deduplicated(self):
        self.assertEqual(_admin_phones(' 255787550399,255787550399 '), (self.phone,))
        self.assertEqual(_admin_phones(''), ())
        for value in ('0748507140', 'hello', '255787550399,', '255 787550399'):
            with self.assertRaises(ConfigurationError):
                _admin_phones(value)

    def test_provider_buttons_and_list_roundtrip(self):
        client = WhatsAppClient(replace(self.settings, dry_run_whatsapp=False))
        for count in (3, 5):
            options = [{'title': f'Option {i}', 'command': menu.ui('main')} for i in range(count)]
            token = 'IBX-'+'A'*32
            with patch.object(client, '_post_graph', return_value={'messages': [{'id': 'wamid.out'}]}) as post:
                client.send_menu(self.phone, 'Choose an option', options, token)
            interactive = post.call_args.args[0]['interactive']
            if count == 3:
                reply = interactive['action']['buttons'][1]['reply']; kind = 'button_reply'
            else:
                reply = interactive['action']['sections'][0]['rows'][1]; kind = 'list_reply'
            payload = {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'field': 'messages', 'value': {
                'metadata': {'phone_number_id': '123'}, 'messages': [{'id': 'wamid.in', 'from': self.phone,
                'timestamp': str(int(time.time())), 'context': {'id': 'wamid.out'}, 'type': 'interactive',
                'interactive': {'type': kind, kind: reply}}]}}]}]}
            response = list(extract_responses(payload, expected_phone_number_id='123'))[0]
            self.assertEqual((response['action'], response['menu_index'], response['reference']), ('menu', 1, token))

    def test_admin_discoverable_in_main_and_guide(self):
        self.send('HEY')
        self.assertIn('Admin controls', self.jobs()[-1]['actions_json'])
        self.click('User guide')
        self.click('Admin controls')
        self.assertIn('does not grant approval', self.client.send_menu.call_args.args[1])
        self.settings = replace(self.settings, whatsapp_admin_phones=())
        self.send('HELP')
        self.assertNotIn('Admin controls', self.jobs()[-1]['actions_json'])

    def test_resume_choice_sends_new_choice_buttons(self):
        from approval_gateway.notifications import process_notifications
        task = baseline.GatewayTests.new_request(self)
        decide(self.settings, self.response(action='rejected', reference=task['gateway_reference_id']))
        self.send('HEY')
        self.click('Continue rejection')
        self.client.send_rejection_choices.return_value = 'wamid.new-choice'
        process_notifications(self.settings, self.client, 25)
        self.client.send_rejection_choices.assert_called_once()
        self.assertEqual(self.client.send_rejection_choices.call_args.args[2], task['gateway_reference_id'])
        self.assertNotIn('original rejection prompt', self.client.send_menu.call_args.args[1])

    def test_main_menu_pauses_and_resume_restores_reason(self):
        task = self.begin_rejection()
        decide(self.settings, self.response('Wrong price', reference=task['gateway_reference_id']))
        self.send('MENU')
        self.send('Just checking the menu')
        with connection(self.settings) as conn:
            row = conn.execute('SELECT * FROM approval_requests').fetchone()
        self.assertEqual(row['rejection_draft'], 'Wrong price')
        self.assertEqual(row['rejection_paused'], 1)
        self.click('Resume rejection')
        self.click('Continue rejection')
        with connection(self.settings) as conn:
            row = conn.execute('SELECT * FROM approval_requests').fetchone()
            payloads = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM decision_repeat_notifications WHERE status='pending'")]
        self.assertEqual(row['rejection_paused'], 0)
        self.assertEqual(row['rejection_draft'], 'Wrong price')
        self.assertTrue(any(p['type'] == 'rejection_review' for p in payloads))

    def test_resume_rechecks_changed_assignment(self):
        self.begin_rejection()
        self.workflow.record_digest = 'changed'
        self.send('HEY')
        self.assertIn('assignment changed', self.client.send_menu.call_args.args[1])
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0], 0)

    def test_multiple_rejections_offer_selection(self):
        first = self.begin_rejection()
        self.workflow.record_digest = '2'
        self.data['workflow_version'] = '2'
        other = baseline.GatewayTests.new_request(self)
        decide(self.settings, self.response(action='rejected', reference=other['gateway_reference_id']))
        self.send('HEY')
        options = json.loads(self.jobs()[-1]['actions_json'])
        self.assertEqual({o['command'].get('task') for o in options if o['command'].get('task')},
                         {first['gateway_reference_id'], other['gateway_reference_id']})

    def test_context_help_never_saves_help_as_reason(self):
        self.begin_rejection()
        self.send('HELP')
        self.send('HEY')
        with connection(self.settings) as conn:
            self.assertIsNone(conn.execute('SELECT rejection_draft FROM approval_requests').fetchone()[0])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0], 0)
