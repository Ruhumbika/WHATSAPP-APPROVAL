import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_gateway as baseline
from approval_gateway.db import connection
from approval_gateway.service_context import discover, selected, select
from approval_gateway.integrations.permissions import IntegrationUnavailable, WorkflowDenied


class ServiceContextTests(unittest.TestCase):
    setUp = baseline.GatewayTests.setUp

    def guard(self, phone=None, service_id=14, name='PETA HOLDING'):
        return SimpleNamespace(directory=SimpleNamespace(
            get_service_details=lambda **kw: (object(), {'phone_number': phone or self.phone,
                '_verified_service': {'id':service_id,'name':name}}),
            source_user_id=lambda user: '42'), _international_phone=lambda phone: phone)

    def test_verified_directory_service_used_and_phone_change_denied(self):
        with patch('approval_gateway.service_context.get_workflow_guard', return_value=self.guard()):
            services, unavailable = discover(self.settings, self.phone)
            self.assertFalse(unavailable)
            self.assertEqual(selected(self.settings, self.phone, services)['name'], 'PETA HOLDING')
        with patch('approval_gateway.service_context.get_workflow_guard', return_value=self.guard(phone='255700000001')):
            self.assertEqual(discover(self.settings, self.phone)[0], [])

    def test_directory_outage_is_not_no_service_claim(self):
        with patch('approval_gateway.service_context.get_workflow_guard', side_effect=IntegrationUnavailable()):
            services, unavailable = discover(self.settings, self.phone)
        self.assertEqual(services, [])
        self.assertTrue(unavailable)

    def test_multiple_services_require_selection_and_revocation_rechecked(self):
        with connection(self.settings) as conn:
            conn.execute("INSERT INTO actor_bindings(system_name,company,directory_uuid,source_user_id,approver_id,active) SELECT 'other',company,directory_uuid,source_user_id,approver_id,active FROM actor_bindings")
        with patch('approval_gateway.service_context.get_workflow_guard',
                   side_effect=lambda system: self.guard(service_id=14 if system=='peta' else 15, name=system.upper())):
            services, _ = discover(self.settings, self.phone)
            self.assertIsNone(selected(self.settings, self.phone, services))
            select(self.settings, self.phone, 'other', 15)
            self.assertEqual(selected(self.settings, self.phone, services)['system'], 'other')
        with patch('approval_gateway.service_context.get_workflow_guard', return_value=self.guard(phone='255700000001')):
            with self.assertRaises(WorkflowDenied):
                select(self.settings, self.phone, 'other', 15)

    def test_typing_payload_is_bounded_and_has_no_business_decision(self):
        from dataclasses import replace
        from approval_gateway.whatsapp import WhatsAppClient
        client = WhatsAppClient(replace(self.settings, dry_run_whatsapp=False))
        with patch.object(client, '_post_graph', return_value={'success': True}) as post:
            client.send_typing('wamid.inbound')
        payload = post.call_args.args[0]
        self.assertEqual(payload['status'], 'read')
        self.assertEqual(payload['typing_indicator'], {'type': 'text'})
        self.assertEqual(post.call_args.kwargs['timeout'], 2)
        self.assertNotIn('to', payload)
