import unittest
from dataclasses import replace
import test_gateway as baseline
from approval_gateway import delivery_monitor as monitor
from approval_gateway.db import connection

class DeliveryMonitorTests(unittest.TestCase):
    def setUp(self):
        baseline.GatewayTests.setUp(self)
        self.settings = replace(self.settings, whatsapp_admin_phones=(self.phone,))

    def payload(self, status, stamp='100', phone=None):
        return {'object':'whatsapp_business_account','entry':[{'changes':[{
            'field':'messages','value':{'metadata':{'phone_number_id':phone or self.settings.whatsapp_phone_number_id},
            'statuses':[{'id':'wamid.test','status':status,'timestamp':stamp,'errors':[{'code':131000}]}]}}]}]}

    def test_duplicate_and_out_of_order_receipts(self):
        monitor.record(self.settings, self.payload('read','200'))
        monitor.record(self.settings, self.payload('sent','100'))
        monitor.record(self.settings, self.payload('read','200'))
        with connection(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM delivery_receipts').fetchone()[0],2)
        self.assertIn('read: 1',monitor.report(self.settings,self.phone))
        self.assertNotIn('sent: 1',monitor.report(self.settings,self.phone))

    def test_wrong_phone_ignored_and_admin_required(self):
        monitor.record(self.settings,self.payload('failed',phone='wrong-number-id'))
        self.assertIn('No receipts',monitor.report(self.settings,self.phone))
        with self.assertRaises(Exception):
            monitor.report(replace(self.settings,whatsapp_admin_phones=()),self.phone)

    def test_failure_code_visible(self):
        monitor.record(self.settings,self.payload('failed'))
        self.assertIn('131000',monitor.report(self.settings,self.phone))
