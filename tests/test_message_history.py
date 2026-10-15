import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
import test_gateway as baseline
from approval_gateway import message_history as history
from approval_gateway.db import connection
from approval_gateway.whatsapp import WhatsAppClient, WhatsAppError
from approval_gateway.integrations.permissions import WorkflowDenied


class HistoryTests(unittest.TestCase):
    def setUp(self):
        baseline.GatewayTests.setUp(self)
        self.settings=replace(self.settings,whatsapp_admin_phones=(self.phone,),dry_run_whatsapp=False)
        self.client=WhatsAppClient(self.settings)

    def test_acceptance_is_journaled_without_action_tokens(self):
        payload={'to':self.phone,'type':'interactive','interactive':{'body':{'text':'Review request'},
                 'action':{'buttons':[{'reply':{'id':'SECRET-ACTION','title':'Approve'}}]}}}
        with patch.object(self.client,'_post_graph_transport',return_value={'messages':[{'id':'wamid.1'}]}):
            self.client._post_graph(payload)
        row=history.rows(self.settings,self.phone)[0]
        self.assertEqual(row['status'],'accepted')
        self.assertIn('Review request',row['content_json'])
        self.assertNotIn('SECRET-ACTION',row['content_json'])

    def test_failure_and_ambiguous_acceptance_are_distinct(self):
        for unknown in [False,True]:
            with patch.object(self.client,'_post_graph_transport',side_effect=WhatsAppError('Failure',delivery_unknown=unknown,api_code=190)):
                with self.assertRaises(WhatsAppError):
                    self.client.send_text(self.phone,'Hello')
        statuses={r['status'] for r in history.rows(self.settings,self.phone)}
        self.assertEqual(statuses,{'failed','unknown'})

    def test_missing_ack_is_unknown(self):
        with patch.object(self.client,'_post_graph_transport',return_value={}):
            with self.assertRaises(WhatsAppError):self.client.send_text(self.phone,'Hello')
        self.assertEqual(history.rows(self.settings,self.phone)[0]['status'],'unknown')

    def test_receipts_override_acceptance_and_filters_and_pagination(self):
        for i in range(3):
            row_id=history.begin(self.settings,{'to':self.phone,'type':'text','text':{'body':str(i)}})
            history.finish(self.settings,row_id,'accepted',f'wamid.{i}')
        with connection(self.settings) as c:
            c.execute("INSERT INTO delivery_receipts VALUES('wamid.2','read',200,NULL,'2026-10-15T00:00:00+00:00')")
            c.execute("INSERT INTO delivery_receipts VALUES('wamid.2','sent',100,NULL,'2026-10-15T00:00:00+00:00')")
        self.assertEqual(len(history.rows(self.settings,self.phone,status='read')),1)
        a=history.rows(self.settings,self.phone,limit=1)[0]
        b=history.rows(self.settings,self.phone,offset=1,limit=1)[0]
        self.assertNotEqual(a['id'],b['id'])
        self.assertEqual(history.rows(self.settings,self.phone,recipient='255700000099'),[])

    def test_legacy_approval_metadata_is_included_without_fabricated_content(self):
        task=baseline.GatewayTests.new_request(self)
        with connection(self.settings) as c:
            c.execute("UPDATE approval_requests SET whatsapp_message_id='wamid.old',delivery_status='sent' WHERE id=?",(task['id'],))
        row=history.rows(self.settings,self.phone,system='peta',reference='REQ-9')[0]
        self.assertEqual(row['message_id'],'wamid.old')
        self.assertIn('Historical metadata only',row['content_json'])

    def test_export_requires_admin_before_opening_file_and_escapes_spreadsheet_formulas(self):
        history.begin(self.settings,{'to':'=DANGEROUS','type':'text','text':{'body':'Private'}})
        path=Path(self.settings.database_path).parent/'report.csv'
        denied=replace(self.settings,whatsapp_admin_phones=())
        with self.assertRaises(WorkflowDenied):history.export_csv(denied,self.phone,path)
        self.assertFalse(path.exists())
        history.export_csv(self.settings,self.phone,path)
        with path.open(encoding='utf-8-sig') as handle:
            rows=list(csv.DictReader(handle))
        self.assertEqual(rows[0]['phone'],"'=DANGEROUS")

    def test_admin_removal_blocks_chat_report(self):
        with self.assertRaises(WorkflowDenied):
            history.report(replace(self.settings,whatsapp_admin_phones=()),self.phone)
