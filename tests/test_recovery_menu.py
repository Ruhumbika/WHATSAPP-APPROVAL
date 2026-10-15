import unittest
from dataclasses import replace
from unittest.mock import patch
import test_chat_menu as fixture
from approval_gateway.db import connection


class RecoveryMenuTests(unittest.TestCase):
    setUp=fixture.ChatMenuTests.setUp
    response=fixture.ChatMenuTests.response
    jobs=fixture.ChatMenuTests.jobs
    send=fixture.ChatMenuTests.send
    click=fixture.ChatMenuTests.click

    def test_trace_preview_cancel_is_read_only_and_confirm_is_durable(self):
        self.send('ADMIN');self.click('Trace and resolve')
        with patch('approval_gateway.recovery.trace',return_value='ERP current assignment'):
            self.send('peta REQ 301')
            self.click('Reconcile request')
            with connection(self.settings) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM workflow_events').fetchone()[0],0)
            self.click('Cancel')
            self.click('Reconcile request')
            self.click('Confirm reconcile')
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM workflow_events').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM gateway_admin_audit').fetchone()[0],1)

    def test_revoked_admin_cannot_apply_reconciliation_confirmation(self):
        self.send('ADMIN');self.click('Trace and resolve')
        with patch('approval_gateway.recovery.trace',return_value='ERP current assignment'):
            self.send('peta REQ 301');self.click('Reconcile request')
        self.settings=replace(self.settings,whatsapp_admin_phones=())
        self.click('Confirm reconcile')
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM workflow_events').fetchone()[0],0)

    def test_history_has_full_message_view(self):
        from approval_gateway.message_history import begin, finish
        mid=begin(self.settings,{'to':self.phone,'type':'text','text':{'body':'Full message body'}})
        finish(self.settings,mid,'accepted','wamid.history')
        self.send('ADMIN');self.click('Message history')
        import json
        options=json.loads(self.jobs()[-1]['actions_json'])
        target=next(o['title'] for o in options if o['command'].get('origin')=='journal')
        self.click(target)
        self.assertIn('Full message body',self.client.send_menu.call_args.args[1])
        self.assertIn('accepted',self.client.send_menu.call_args.args[1])
