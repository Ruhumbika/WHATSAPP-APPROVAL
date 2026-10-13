import unittest
import time
from dataclasses import replace
from unittest.mock import Mock, patch

import test_progress as progress_baseline
import test_rejection as rejection_baseline
from approval_gateway.db import connection
from approval_gateway.meta import extract_responses
from approval_gateway.notifications import process_notifications
from approval_gateway.progress import handle_progress
from approval_gateway.upgrade import migrate
from approval_gateway.whatsapp import WhatsAppClient, WhatsAppError


class WorkflowMessageTests(unittest.TestCase):
    setUp = progress_baseline.ProgressTests.setUp
    new_request = progress_baseline.ProgressTests.new_request
    fixture = progress_baseline.ProgressTests.fixture
    queue_count = progress_baseline.ProgressTests.queue_count

    def incoming(self, payload):
        return list(extract_responses({"object": "whatsapp_business_account", "entry": [{"changes": [{
            "field": "messages", "value": {"metadata": {"phone_number_id": "123"}, "messages": [{
                "id": "button-1", "from": self.phone, "timestamp": str(int(time.time())),
                "type": "button", "button": {"payload": payload},
            }]}
        }]}]}, expected_phone_number_id="123"))[0]

    def test_status_template_and_button_full_journey_round_trip(self):
        task = self.fixture()
        client = WhatsAppClient(replace(self.settings, dry_run_whatsapp=False))
        with patch('approval_gateway.progress.get_workflow_guard', return_value=self.guard), patch('approval_gateway.progress.PetaClient._get', return_value={'success': True, 'data': self.data}), patch.object(client, '_post_graph', return_value={'messages': [{'id': 'wamid.progress'}]}) as post:
            handle_progress(self.settings, self.reply)
            process_notifications(self.settings, client, 10)
            payload = post.call_args[0][0]
            self.assertEqual(payload['template']['name'], 'workflow_progress_update')
            components = payload['template']['components']
            self.assertEqual(len(components[0]['parameters']), 7)
            self.assertIn('john.john', components[0]['parameters'][4]['text'])
            button = components[1]['parameters'][0]['payload']
            self.assertEqual(button, 'progress_' + task['gateway_reference_id'])
            response = self.incoming(button)
            handle_progress(self.settings, response)
            process_notifications(self.settings, client, 10)
            self.assertEqual(post.call_args[0][0]['type'], 'text')
            self.assertIn('Approval journey', post.call_args[0][0]['text']['body'])
            self.assertFalse(handle_progress(self.settings, response)['processed'])
            process_notifications(self.settings, client, 10)
            self.assertEqual(post.call_count, 2)

    def test_directory_phone_change_while_queued_prevents_delivery(self):
        self.fixture()
        client = Mock()
        with patch('approval_gateway.progress.get_workflow_guard', return_value=self.guard), patch('approval_gateway.progress.PetaClient._get', return_value={'success': True, 'data': self.data}):
            handle_progress(self.settings, self.reply)
            self.guard._international_phone = lambda phone: '255700000099'
            process_notifications(self.settings, client, 10)
        client.send_progress.assert_not_called()
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT status FROM decision_repeat_notifications').fetchone()[0], 'failed')

    def test_provider_unknown_is_not_automatically_resent(self):
        self.fixture()
        client = Mock()
        client.send_progress.side_effect = WhatsAppError('Lost response', delivery_unknown=True)
        with patch('approval_gateway.progress.get_workflow_guard', return_value=self.guard), patch('approval_gateway.progress.PetaClient._get', return_value={'success': True, 'data': self.data}):
            handle_progress(self.settings, self.reply)
            process_notifications(self.settings, client, 10)
            process_notifications(self.settings, client, 10)
        self.assertEqual(client.send_progress.call_count, 1)
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT status FROM decision_repeat_notifications').fetchone()[0], 'unknown')

    def test_migration_is_repeatable_and_preserves_queued_reply(self):
        self.fixture()
        with patch('approval_gateway.progress.get_workflow_guard', return_value=self.guard), patch('approval_gateway.progress.PetaClient._get', return_value={'success': True, 'data': self.data}):
            handle_progress(self.settings, self.reply)
        migrate(self.settings)
        migrate(self.settings)
        self.assertEqual(self.queue_count(), 1)

    def test_payment_advice_template_uses_correct_reference(self):
        task = self.fixture()
        with connection(self.settings) as conn:
            conn.execute("UPDATE approval_requests SET request_type='payment_advice_approval' WHERE id=?", (task['id'],))
        self.data['request_type'] = 'payment_advice_approval'
        self.reply['text'] = 'STATUS PA 297'
        client = Mock()
        client.send_progress.return_value = 'wamid.pa'
        with patch('approval_gateway.progress.get_workflow_guard', return_value=self.guard), patch('approval_gateway.progress.PetaClient._get', return_value={'success': True, 'data': self.data}):
            handle_progress(self.settings, self.reply)
            process_notifications(self.settings, client, 10)
        self.assertEqual(client.send_progress.call_args[0][2][0], 'Payment Advice #297')


class RejectionLayoutTests(unittest.TestCase):
    setUp = rejection_baseline.RejectionTests.setUp
    new_request = rejection_baseline.RejectionTests.new_request
    response = rejection_baseline.RejectionTests.response
    send = rejection_baseline.RejectionTests.send
    start = rejection_baseline.RejectionTests.start
    callbacks = rejection_baseline.RejectionTests.callbacks

    def test_choice_buttons_keep_reference_hidden_and_route_to_reason(self):
        task = self.start()
        client = WhatsAppClient(replace(self.settings, dry_run_whatsapp=False))
        with patch.object(client, '_post_graph', return_value={'messages': [{'id': 'wamid.choice'}]}) as post:
            process_notifications(self.settings, client, 10)
        interactive = post.call_args[0][0]['interactive']
        self.assertNotIn('AGR-', interactive['body']['text'])
        self.assertIn('\n\n', interactive['body']['text'])
        buttons = interactive['action']['buttons']
        self.assertEqual(len(buttons), 3)
        self.assertTrue(all(len(button['reply']['title']) <= 20 for button in buttons))
        choice = buttons[0]['reply']['id']
        response = list(extract_responses({'object':'whatsapp_business_account','entry':[{'changes':[{
            'field':'messages','value':{'metadata':{'phone_number_id':'123'},'messages':[{
                'id':'selected','from':self.phone,'type':'interactive','context':{'id':'wamid.choice'},
                'interactive':{'type':'button_reply','button_reply':{'id':choice}},
            }]}
        }]}]}, expected_phone_number_id='123'))[0]
        from approval_gateway.service import decide
        decide(self.settings, response)
        self.assertEqual(self.callbacks(), [])
        self.send(task, 'conversation', 'Correct the quantities', 'reason')
        self.assertEqual(self.callbacks()[0]['rejection']['mode'], 'correction')


class ItemSummaryTests(unittest.TestCase):
    def test_compact_preview_preserves_quantities_totals_and_source(self):
        from approval_gateway.integrations.peta import _detailed_message_fields
        import copy
        record = {'id': 298, 'company_name': 'PETA', 'items': [{
            'currency': 'TZS', 'amount_before_vat': '1000', 'vat_amount': '180', 'total_amount': '1180',
            'materials': [
                {'item_name': 'Hard drive', 'quantity': '1.75', 'rate': '400', 'unit': 'Piece'},
                {'item_name': 'Cable', 'quantity': '2', 'rate': '100', 'unit': 'Piece'},
                {'item_name': 'Adapter', 'quantity': '1', 'rate': '100', 'unit': 'Piece'},
            ],
        }]}
        before = copy.deepcopy(record)
        result = _detailed_message_fields(record)
        self.assertEqual(result['item_summary'], 'Hard drive (1.75 Piece); Cable (2 Piece); +1 more in PDF')
        self.assertEqual(result['total_display'], 'TZS 1,180.00')
        self.assertEqual(record, before)
