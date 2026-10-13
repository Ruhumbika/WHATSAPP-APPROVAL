import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import test_chat_menu as fixture
from test_rejection import confirm_review
from approval_gateway import rejection_flow as flow
from approval_gateway.db import connection
from approval_gateway.meta import extract_responses
from approval_gateway.service import ServiceError, decide
from approval_gateway.whatsapp import WhatsAppClient, WhatsAppError


class RejectionFlowTests(unittest.TestCase):
    setUp = fixture.ChatMenuTests.setUp
    response = fixture.ChatMenuTests.response
    jobs = fixture.ChatMenuTests.jobs
    send = fixture.ChatMenuTests.send
    click = fixture.ChatMenuTests.click
    begin_rejection = fixture.ChatMenuTests.begin_rejection

    def launch(self):
        self.settings = replace(self.settings, whatsapp_rejection_flow_enabled=True, whatsapp_rejection_flow_id='123456')
        self.client.send_rejection_flow.return_value = 'wamid.flow'
        task = self.begin_rejection()
        self.send('HEY')
        self.click('Open rejection form')
        with connection(self.settings) as conn:
            launch = dict(conn.execute('SELECT * FROM rejection_flows').fetchone())
        self.assertEqual(self.jobs()[-1]['status'], 'simulated')
        reply = self.response(action='flow_reply', context_id='wamid.flow',
            flow_data={'flow_token': launch['token'], 'mode': 'correction', 'reason': 'Correct the quantities'})
        return task, reply

    def current(self, task):
        with connection(self.settings) as conn:
            return dict(conn.execute('SELECT * FROM approval_requests WHERE id=?', (task['id'],)).fetchone())

    def callbacks(self):
        with connection(self.settings) as conn:
            return conn.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0]

    def test_draft_requires_final_confirmation_and_replay_cannot_commit_twice(self):
        task, reply = self.launch()
        self.assertTrue(flow.handle(self.settings, reply)['processed'])
        self.assertEqual(self.current(task)['rejection_draft'], 'Correct the quantities')
        self.assertEqual(self.callbacks(), 0)
        self.assertEqual(flow.handle(self.settings, reply)['reason'], 'duplicate_message')
        with self.assertRaises(ServiceError):
            flow.handle(self.settings, dict(reply, message_id='replay'))
        confirm_review(self.settings, task, self.phone)
        self.assertEqual(self.callbacks(), 1)
        self.assertEqual(self.current(task)['status'], 'rejected')

    def test_wrong_sender_context_and_unknown_token(self):
        task, reply = self.launch()
        variants = [dict(reply, phone='255700000000'), dict(reply, context_id='wamid.other'),
                    dict(reply, flow_data=dict(reply['flow_data'], flow_token='RFL-'+'0'*32))]
        for bad in variants:
            with self.subTest(bad=bad), self.assertRaises(ServiceError):
                flow.handle(self.settings, bad)
        self.assertIsNone(self.current(task)['rejection_draft'])
        self.assertEqual(self.callbacks(), 0)

    def test_expired_form(self):
        task, reply = self.launch()
        with connection(self.settings) as conn:
            conn.execute("UPDATE rejection_flows SET expires_at='2000-01-01T00:00:00+00:00'")
        with self.assertRaises(ServiceError):
            flow.handle(self.settings, reply)
        self.assertEqual(self.callbacks(), 0)

    def test_chat_edit_and_cancel_invalidate_form(self):
        task, reply = self.launch()
        decide(self.settings, self.response('A newer chat reason', reference=task['gateway_reference_id']))
        with self.assertRaises(ServiceError):
            flow.handle(self.settings, reply)
        self.assertEqual(self.current(task)['rejection_draft'], 'A newer chat reason')
        decide(self.settings, self.response('CANCEL', reference=task['gateway_reference_id']))
        with self.assertRaises(ServiceError):
            flow.handle(self.settings, reply)
        self.assertEqual(self.callbacks(), 0)

    def test_other_reviewer_or_snapshot_change_blocks_form(self):
        task, reply = self.launch()
        with patch('approval_gateway.service._preflight_decision', side_effect=ServiceError('Assignment changed', 403)):
            with self.assertRaises(ServiceError):
                flow.handle(self.settings, reply)
        with connection(self.settings) as conn:
            conn.execute("UPDATE approval_requests SET status='cancelled' WHERE id=?", (task['id'],))
        with self.assertRaises(ServiceError):
            flow.handle(self.settings, reply)
        self.assertEqual(self.callbacks(), 0)

    def test_live_check_failure_leaves_token_retriable(self):
        task, reply = self.launch()
        with patch('approval_gateway.service._preflight_decision', side_effect=ServiceError('Unavailable', 503)):
            with self.assertRaises(ServiceError):
                flow.handle(self.settings, reply)
        self.assertTrue(flow.handle(self.settings, reply)['processed'])

    def test_invalid_fields_do_not_consume_form(self):
        task, reply = self.launch()
        for data in (None, [], dict(reply['flow_data'], mode='approve'), dict(reply['flow_data'], reason=' '),
                     dict(reply['flow_data'], reason='x'*2001)):
            with self.subTest(data=data), self.assertRaises(ServiceError):
                flow.handle(self.settings, dict(reply, flow_data=data))
        self.assertTrue(flow.handle(self.settings, reply)['processed'])

    def test_disabled_keeps_existing_buttons_and_rejects_old_forms(self):
        self.begin_rejection()
        self.send('HEY')
        self.assertNotIn('Open rejection form', self.jobs()[-1]['actions_json'])
        with self.assertRaises(ServiceError):
            flow.handle(self.settings, self.response(action='flow_reply', flow_data={}))

    def test_graph_payload_is_published_and_bound_to_token(self):
        _, reply = self.launch()
        client = WhatsAppClient(replace(self.settings, dry_run_whatsapp=False))
        with patch.object(client, '_post_graph', return_value={'messages':[{'id':'wamid.sent'}]}) as post:
            self.assertEqual(client.send_rejection_flow(self.phone, 'Draft only', reply['flow_data']['flow_token'], 'REQ 312'), 'wamid.sent')
        params = post.call_args.args[0]['interactive']['action']['parameters']
        self.assertEqual(params['mode'], 'published')
        self.assertEqual(params['flow_token'], reply['flow_data']['flow_token'])
        self.assertEqual(params['flow_action_payload'], {'screen':'REJECTION_FORM','data':{'request_label':'REQ 312', 'request_summary':'Request details: see request PDF', 'request_items':'Item details: see request PDF.'}})

    def test_unknown_send_is_not_retried_or_accepted_as_delivered(self):
        from approval_gateway.inbox import process_inbox
        self.settings = replace(self.settings, whatsapp_rejection_flow_enabled=True, whatsapp_rejection_flow_id='123')
        self.begin_rejection()
        self.send('HEY')
        self.client.send_rejection_flow.side_effect = WhatsAppError('Timeout', delivery_unknown=True)
        self.click('Open rejection form')
        self.assertEqual(self.jobs()[-1]['status'], 'unknown')
        process_inbox(self.settings, self.client, 25)
        self.assertEqual(self.client.send_rejection_flow.call_count, 1)

    def test_webhook_extraction_does_not_interpret_form_as_chat_reason(self):
        message = {'id':'wamid.reply','from':self.phone,'type':'interactive','context':{'id':'wamid.flow'},
                   'interactive':{'type':'nfm_reply','nfm_reply':{'response_json':json.dumps({'flow_token':'RFL-'+'A'*32,'mode':'final','reason':'Wrong quantity'})}}}
        def extract(msg):
            return list(extract_responses({'object':'whatsapp_business_account','entry':[{'changes':[{'field':'messages','value':{'metadata':{'phone_number_id':'123'},'messages':[msg]}}]}]}, expected_phone_number_id='123'))
        replies = extract(message)
        self.assertEqual(replies[0]['action'], 'flow_reply')
        self.assertEqual(replies[0]['flow_data']['mode'], 'final')
        message['interactive']['nfm_reply']['response_json'] = '{bad'
        self.assertIsNone(extract(message)[0]['flow_data'])

    def test_flow_file_completion_has_no_client_supplied_identity(self):
        document = json.loads((Path(__file__).resolve().parents[1]/'flows/rejection-review.json').read_text())
        screen = document['screens'][0]
        fields = screen['layout']['children'][0]['children']
        self.assertEqual(screen['id'], 'REJECTION_FORM')
        navigate = fields[-1]['on-click-action']
        self.assertEqual(navigate['name'], 'navigate')
        review = document['screens'][1]
        self.assertEqual(navigate['next']['name'], review['id'])
        self.assertEqual(set(navigate['payload']), set(review['data']))
        complete = review['layout']['children'][-1]['on-click-action']
        self.assertEqual(complete, {'name':'complete', 'payload':{'mode':'${data.mode}','reason':'${data.reason}'}})

    def test_summary_is_scoped_to_task_and_missing_amount_is_not_invented(self):
        task, _ = self.launch()
        task = self.current(task)
        display = flow.summary_data(task)
        self.assertIn('Company: PETA / Company 77', display['request_summary'])
        self.assertIn('Amount: See request PDF', display['request_summary'])
        self.assertIn('Requested by: Daniel Hussein', display['request_summary'])
        sent = self.client.send_rejection_flow.call_args.args
        self.assertEqual(sent[3:5], (display['request_label'], display['request_summary']))
        payload = json.loads(task['payload_json'])
        payload['record_snapshot'] = {'company_name':'Another Company', 'requested_by':'Another Requester', 'total_display':'USD 500.00'}
        display = flow.summary_data(dict(task, payload_json=json.dumps(payload)))
        self.assertIn('Company: Another Company', display['request_summary'])
        self.assertIn('Amount: USD 500.00', display['request_summary'])
        self.assertIn('Requested by: Another Requester', display['request_summary'])

    def test_completion_display_fields_cannot_redirect_decision(self):
        task, reply = self.launch()
        reply['flow_data'].update(request_label='REQ OTHER', company='999', task_id=999,
                                  request_summary='Forged company', amount='1')
        flow.handle(self.settings, reply)
        self.assertEqual(self.current(task)['rejection_draft'], 'Correct the quantities')
        self.assertEqual(self.current(task)['company'], '77')
        self.assertEqual(self.callbacks(), 0)

    def test_graph_payload_includes_server_summary(self):
        client = WhatsAppClient(replace(self.settings, whatsapp_rejection_flow_enabled=True,
            whatsapp_rejection_flow_id='123', dry_run_whatsapp=False))
        with patch.object(client, '_post_graph', return_value={'messages':[{'id':'wamid.sent'}]}) as post:
            client.send_rejection_flow(self.phone, 'Draft only', 'RFL-'+'A'*32,
                                       'REQ 99', 'Company: Example\nAmount: USD 50')
        data = post.call_args.args[0]['interactive']['action']['parameters']['flow_action_payload']['data']
        self.assertEqual(data, {'request_label':'REQ 99', 'request_summary':'Company: Example\nAmount: USD 50', 'request_items':'Item details: see request PDF.'})

    def test_readiness_only_reads_selected_fields_and_rejects_url_injection(self):
        from approval_gateway.flow_readiness import inspect
        from unittest.mock import MagicMock
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"id":"123", "status":"DRAFT", "validation_errors":[], "health_status":{"can_send_message":"BLOCKED"}, "ignored":"value"}'
        with patch('approval_gateway.flow_readiness.urllib.request.build_opener', return_value=opener):
            result = inspect(replace(self.settings, whatsapp_access_token='test-only'), '123')
            self.assertEqual(result['status'], 'DRAFT')
            self.assertNotIn('ignored', result)
            self.assertEqual(opener.open.call_args.args[0].get_method(), 'GET')
            with self.assertRaises(ValueError):
                inspect(self.settings, '123/other')

    def test_permission_is_checked_again_on_final_submit(self):
        task, reply = self.launch()
        flow.handle(self.settings, reply)
        with patch('approval_gateway.service._preflight_decision', side_effect=ServiceError('Another reviewer completed this step', 403)):
            with self.assertRaises(ServiceError):
                confirm_review(self.settings, task, self.phone)
        self.assertEqual(self.callbacks(), 0)

    def test_disabled_by_default_and_enabled_requires_id(self):
        from approval_gateway.config import _validate_settings, ConfigurationError
        self.assertFalse(self.settings.whatsapp_rejection_flow_enabled)
        with self.assertRaises(ConfigurationError):
            _validate_settings(replace(self.settings, whatsapp_rejection_flow_enabled=True, whatsapp_rejection_flow_id=''))


class FlowItemsTests(unittest.TestCase):
    def test_three_materials_then_exact_remaining_count(self):
        import copy
        record = {'company_name':'Example', 'id':9, 'items':[{
            'currency':'TZS', 'amount_before_vat':'100', 'vat_amount':'18', 'total_amount':'118',
            'materials':[{'item_name':f'Item {i}', 'quantity':'2', 'rate':'10'} for i in range(5)]}]}
        before = copy.deepcopy(record)
        result = flow.items_preview(record)
        self.assertIn('Items (5)', result)
        self.assertIn('3. Item 2', result)
        self.assertNotIn('Item 3', result)
        self.assertIn('+ 2 more items', result)
        self.assertIn('2 × TZS 10.00 = TZS 20.00 (before adjustments)', result)
        task = {'source_system':'peta', 'company':'1', 'source_reference_id':'9',
                'request_type':'requisition_approval', 'payload_json':json.dumps({'record_snapshot':record})}
        self.assertIn('TZS 118.00', flow.summary_data(task)['request_summary'])
        self.assertEqual(record, before)

    def test_missing_prices_accounts_and_currency_are_not_invented(self):
        record = {'items':[{'materials':[{'item_name':'Cable', 'quantity':'2', 'rate':'NaN'}],
                            'accounts':[{'account_name':'Transport'}]}]}
        result = flow.items_preview(record)
        self.assertIn('Items (2)', result)
        self.assertIn('Transport', result)
        self.assertNotIn('more items', result)
        self.assertNotIn('NaN', result)
        self.assertNotIn('TZS', result)
        self.assertEqual(flow.items_preview({}), 'Item details: see request PDF.')

    def test_items_only_appear_on_first_screen(self):
        doc = json.loads((Path(__file__).resolve().parents[1]/'flows/rejection-review.json').read_text())
        self.assertIn('request_items', doc['screens'][0]['data'])
        self.assertNotIn('request_items', json.dumps(doc['screens'][1]))
