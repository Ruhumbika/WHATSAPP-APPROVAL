"""Payment Advice event/decision regression tests with mocked ERP and Directory."""
import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4
import test_workflow_integration as baseline
from approval_gateway.integrations.payment_advice import (
    PaymentAdviceClient, PaymentAdviceGuard, PetaWorkflowRouter, REQUEST_TYPE)
from approval_gateway.integrations.peta import PetaWorkflowGuard
from approval_gateway.integrations.registry import configure_workflow_guards
from approval_gateway.integrations.permissions import IntegrationUnavailable, WorkflowDenied
from approval_gateway.workflow_events import accept_workflow_event
from approval_gateway.db import connection


class PaymentAdviceTests(unittest.TestCase):
    task = baseline.WorkflowIntegrationTests.task
    reject_with_reason = baseline.WorkflowIntegrationTests.reject_with_reason
    reply = baseline.WorkflowIntegrationTests.reply
    run_worker = baseline.WorkflowIntegrationTests.run_worker
    directory_response = baseline.WorkflowIntegrationTests.directory_response

    def setUp(self):
        baseline.WorkflowIntegrationTests.setUp(self)
        self.settings = replace(self.settings, whatsapp_template_name='workflow_approval_document')
        self.record = {
            'id': 9, 'company_id': 77, 'company_name': 'Test Company',
            'status': 'PENDING', 'is_closed': False, 'date': '2026-10-09',
            'remarks': 'Parts', 'requested_by': 'Workshop', 'sources': [],
            'items': [{'payment_request_item_id': 1, 'currency': 'TZS',
                       'amount': '250000.00', 'review_amount': '250000.00',
                       'description': 'Parts'}],
        }
        old = self.peta
        directory = baseline.PetaDirectoryClient('https://directory.example/api/v1/client', 'fake', service_id=4, shared_user_ids=True)
        directory._get = Mock(side_effect=self.directory_response)
        self.peta = PaymentAdviceClient('https://peta.example/api/v1', 's'*32,
                                       document_access_token='s'*32)
        self.peta._get = Mock(side_effect=self.peta_response)
        self.guard = PaymentAdviceGuard(self.peta, directory, '255')
        self.router = PetaWorkflowRouter(PetaWorkflowGuard(old, directory, '255'), self.guard)
        configure_workflow_guards({'peta': self.router})

    def peta_response(self, path):
        if path.endswith('/details') and 'payment-advices/9/' in path:
            return {'success': True, 'data': copy.deepcopy(self.record)}
        if path.endswith('/approval-chain') and 'payment-advices/9/' in path:
            payload = baseline.WorkflowIntegrationTests.peta_response(self, 'requisitions/9/approval-chain')
            payload['data']['payment_request_id'] = payload['data'].pop('requisition_id')
            return payload
        raise AssertionError(path)

    def trigger(self, **fields):
        data = {'event_id': str(uuid4()), 'source_system': 'peta',
                'request_type': REQUEST_TYPE, 'reference_id': '9'}
        data.update(fields)
        return accept_workflow_event(self.settings, 'test-key', data)

    test_duplicate_event = baseline.WorkflowIntegrationTests.test_duplicate_event_and_repeated_trigger_do_not_duplicate_tasks
    test_stale_step = baseline.WorkflowIntegrationTests.test_stale_task_is_not_delivered
    test_wrong_sender_and_changed_snapshot = baseline.WorkflowIntegrationTests.test_wrong_sender_and_changed_record_are_denied
    test_duplicate_click = baseline.WorkflowIntegrationTests.test_duplicate_decision_has_one_callback
    test_opposite_decision = baseline.WorkflowIntegrationTests.test_opposite_decision_keeps_one_confirmation_and_callback
    test_pdf_upload_and_template = baseline.WorkflowIntegrationTests.test_document_template_uses_pdf_header_and_current_step

    def test_event_and_next_level(self):
        self.trigger(); self.run_worker()
        self.assertEqual(self.task()['request_type'], REQUEST_TYPE)
        payload = json.loads(self.task()['payload_json'])
        self.assertEqual(payload['payment_request_id'], 9)
        self.assertNotIn('requisition_id', payload)
        self.level = 2
        self.trigger(); self.run_worker()
        self.assertEqual(self.task()['step_id'], '12')
        self.assertEqual(self.task()['actor_user_id'], '4')

    def test_terminal_records_create_no_tasks(self):
        for status in ('APPROVED', 'PARTIALLY_APPROVED', 'REJECTED', 'POSTED', 'CANCELLED', 'CLOSED', 'DRAFT'):
            self.record.update(status=status, is_closed=True)
            self.assertEqual(self.guard.resolve_workflow_event({
                'source_system': 'peta', 'request_type': REQUEST_TYPE, 'reference_id': '9'}), ())

    def test_invalid_amounts_fail_closed(self):
        for amount in ('NaN', '-1', True, '250001'):
            self.record['items'][0]['review_amount'] = amount
            with self.assertRaises(IntegrationUnavailable):
                self.peta.get_requisition(9)

    def test_missing_credential(self):
        client = PaymentAdviceClient('https://peta.example/api/v1')
        with self.assertRaises(WorkflowDenied):
            client.get_requisition(9)

    def test_unknown_type_and_company_are_denied(self):
        with self.assertRaises(WorkflowDenied):
            self.router.resolve_workflow_event({'request_type': 'other'})
        with self.assertRaises(WorkflowDenied):
            self.guard.authorize({'request_type': REQUEST_TYPE, 'reference_id': '9', 'company': '2'})

    def test_detailed_template_and_filename(self):
        tasks = self.guard.resolve_workflow_event({'source_system': 'peta',
                        'request_type': REQUEST_TYPE, 'reference_id': '9'})
        task = tasks[0]
        request = {'request_type': REQUEST_TYPE, 'source_reference_id': '9',
                   'payload_json': json.dumps(task['payload'])}
        details = self.guard.prepare_delivery(replace(self.settings,
            whatsapp_template_name='workflow_approval_detail_document_v2'), request)
        self.assertEqual(details['request_title'], 'Payment Advice')
        self.assertEqual(details['document_filename'], 'payment-advice-9.pdf')
        self.assertEqual(details['total_display'], 'TZS 250,000.00')
        self.assertIn('Not separately stated', details['vat_display'])

    def test_requisition_specific_template_denied(self):
        with self.assertRaises(WorkflowDenied):
            self.guard.prepare_delivery(replace(self.settings,
                whatsapp_template_name='requisition_approval_document'), {'request_type': REQUEST_TYPE})

    def test_rejection_requires_choice_and_reason(self):
        self.trigger(); self.run_worker()
        task = self.task()
        self.reply(task, action='rejected', text='Reject')
        self.assertEqual(self.task()['status'], 'pending')
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0], 0)
            prompt = c.execute('SELECT body FROM decision_repeat_notifications ORDER BY id DESC').fetchone()[0]
        self.assertIn('Payment Advice', prompt)
        self.reply(task, action='conversation', text='2')
        self.reply(task, action='conversation', text='Incorrect beneficiary details')
        with connection(self.settings) as c:
            payload = json.loads(c.execute('SELECT payload_json FROM callback_attempts').fetchone()[0])
        self.assertEqual(payload['request_type'], REQUEST_TYPE)
        self.assertEqual(payload['rejection'], {'mode': 'final', 'reason': 'Incorrect beneficiary details'})

    def test_correction_choice_and_cancel(self):
        self.trigger(); self.run_worker()
        task = self.task()
        self.reply(task, action='rejected', text='Reject')
        self.reply(task, action='conversation', text='1')
        self.reply(task, action='conversation', text='CANCEL')
        self.assertEqual(self.task()['status'], 'pending')
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM callback_attempts').fetchone()[0], 0)
        self.reply(task, action='rejected', text='Reject')
        self.reply(task, action='conversation', text='1')
        self.reply(task, action='conversation', text='Correct the requested amount')
        with connection(self.settings) as c:
            payload = json.loads(c.execute('SELECT payload_json FROM callback_attempts').fetchone()[0])
        self.assertEqual(payload['rejection']['mode'], 'correction')
