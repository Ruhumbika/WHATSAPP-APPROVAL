import json
import unittest
import test_gateway as baseline
from approval_gateway.db import connection
from approval_gateway.service import decide, ServiceError
from approval_gateway.upgrade import migrate

class RejectionTests(unittest.TestCase):
    setUp = baseline.GatewayTests.setUp
    new_request = baseline.GatewayTests.new_request
    response = baseline.GatewayTests.response
    read = baseline.GatewayTests.read

    def send(self, task, action, text, message_id, **fields):
        return decide(self.settings, self.response(task, action=action, text=text, message_id=message_id, **fields))

    def callbacks(self):
        with connection(self.settings) as c:
            return [json.loads(row[0]) for row in c.execute('SELECT payload_json FROM callback_attempts')]

    def start(self):
        task = self.new_request()
        self.send(task, 'rejected', 'Reject', 'start')
        return task

    def choose(self, task, mode='2'):
        self.send(task, 'conversation', mode, 'choice')

    def test_requires_choice_and_reason(self):
        task = self.start()
        self.assertEqual(self.read(task)['status'], 'pending')
        self.assertEqual(self.callbacks(), [])
        self.choose(task)
        self.assertEqual(self.callbacks(), [])
        self.send(task, 'conversation', 'Incorrect prices', 'reason')
        payload=self.callbacks()[0]
        self.assertEqual(payload['rejection'], {'mode':'final','reason':'Incorrect prices'})
        self.assertEqual(payload['evidence']['message_id'], 'reason')

    def test_correction(self):
        task=self.start(); self.choose(task,'1')
        self.send(task,'conversation','Correct quantities','reason')
        self.assertEqual(self.callbacks()[0]['rejection']['mode'],'correction')

    def test_cancel(self):
        task=self.start(); self.choose(task,'1')
        self.send(task,'conversation','CANCEL','cancel')
        self.assertEqual(self.read(task)['status'],'pending')
        self.assertFalse(self.send(task,'conversation','Late reason','late')['processed'])
        self.assertEqual(self.callbacks(),[])

    def test_duplicate_reason(self):
        task=self.start(); self.choose(task)
        self.send(task,'conversation','Wrong quantities','reason')
        self.assertFalse(self.send(task,'conversation','Wrong quantities','reason')['processed'])
        self.assertEqual(len(self.callbacks()),1)

    def test_unknown_reference_does_not_fall_back(self):
        task=self.start(); self.choose(task)
        result=self.send(task,'conversation','Wrong details','reason',reference='AGR-UNKNOWN')
        self.assertFalse(result['processed']); self.assertEqual(self.callbacks(),[])

    def test_wrong_phone(self):
        task=self.start()
        with self.assertRaises(ServiceError):
            self.send(task,'conversation','2','choice',phone='255700000001')
        self.assertEqual(self.callbacks(),[])

    def test_changed_erp_record(self):
        task=self.start(); self.choose(task)
        self.workflow.record_digest='changed'
        with self.assertRaises(ServiceError):
            self.send(task,'conversation','Wrong total','reason')
        self.assertEqual(self.callbacks(),[])

    def test_invalid_reason(self):
        task=self.start(); self.choose(task)
        self.send(task,'conversation','no','reason')
        self.assertEqual(self.callbacks(),[])

    def test_plain_text_single_conversation(self):
        task=self.start()
        self.send(task,'conversation','1','choice',reference=None)
        self.send(task,'conversation','Wrong details','reason',reference=None)
        self.assertEqual(len(self.callbacks()),1)

    def test_prompt_context_preserved(self):
        task=self.start()
        with connection(self.settings) as c:
            c.execute("UPDATE decision_repeat_notifications SET message_id='wamid.prompt',status='sent' WHERE inbound_message_id='start'")
        self.send(task,'conversation','2','choice',reference=None,context_id='wamid.prompt')
        self.send(task,'conversation','Wrong details','reason',reference=None,context_id='wamid.prompt')
        self.assertEqual(self.callbacks()[0]['evidence']['context_id'],'wamid.prompt')

    def test_approve_closes_conversation(self):
        task=self.start()
        self.send(task,'approved','Approve','approve')
        self.assertEqual(self.read(task)['status'],'approved')
        self.assertFalse(self.send(task,'conversation','Wrong details','late')['processed'])
        self.assertEqual(len(self.callbacks()),1)

    def test_migration_is_additive(self):
        task=self.start(); migrate(self.settings); migrate(self.settings)
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT rejection_phase FROM approval_requests WHERE id=?',(task['id'],)).fetchone()[0],'choice')

    def test_unrelated_context_with_explicit_reference_is_rejected(self):
        task=self.start()
        with self.assertRaises(ServiceError):
            self.send(task,'conversation','2','choice',context_id='unrelated-message')
        self.assertEqual(self.callbacks(),[])

    def test_expired_conversation_does_not_submit_reason(self):
        task=self.start(); self.choose(task)
        with connection(self.settings) as c:
            c.execute("UPDATE approval_requests SET rejection_expires_at='2000-01-01T00:00:00+00:00'")
        self.assertFalse(self.send(task,'conversation','Wrong total','reason')['processed'])
        self.assertEqual(self.callbacks(),[])

    def test_ambiguous_plain_reply_does_not_choose_any_task(self):
        task=self.start()
        self.workflow.record_digest='2'
        self.data['workflow_version']='2'
        other=self.new_request()
        with connection(self.settings) as c:
            original=dict(c.execute('SELECT * FROM approval_requests WHERE id=?',(task['id'],)).fetchone())
            c.execute("UPDATE approval_requests SET rejection_phase='choice',rejection_expires_at=?,rejection_updated_at=? WHERE id=?", (original['rejection_expires_at'],original['rejection_updated_at'],other['id']))
        result=self.send(task,'conversation','2','ambiguous',reference=None)
        self.assertFalse(result['processed'])
        self.assertEqual(self.callbacks(),[])

    def test_superseded_pending_prompts_are_not_sent_late(self):
        task=self.start(); self.choose(task)
        with connection(self.settings) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM decision_repeat_notifications WHERE status='pending'").fetchone()[0],1)
        self.send(task,'conversation','Wrong total','reason')
        with connection(self.settings) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM decision_repeat_notifications WHERE status='pending'").fetchone()[0],0)

    def test_correction_confirmation_waits_for_erp_applied_result(self):
        from approval_gateway.notifications import enqueue_notification
        from approval_gateway.db import utc_now
        task=self.start(); self.choose(task,'1')
        self.send(task,'conversation','Correct quantities','reason')
        with connection(self.settings) as c:
            request=dict(c.execute('SELECT * FROM approval_requests WHERE id=?',(task['id'],)).fetchone())
            enqueue_notification(c,request,'result',utc_now())
            self.assertEqual(c.execute("SELECT COUNT(*) FROM decision_notifications WHERE kind='result'").fetchone()[0],0)
            request['execution_status']='applied'
            enqueue_notification(c,request,'result',utc_now())
            body=c.execute("SELECT body FROM decision_notifications WHERE kind='result'").fetchone()[0]
            self.assertIn('PETA returned requisition',body)
            self.assertIn('Correct quantities',body)
            self.assertIn('new approval cycle',body)

    def legacy_conversation(self, task, phone=None):
        from approval_gateway.db import utc_now
        from datetime import datetime, timedelta
        now=utc_now()
        expires=(datetime.fromisoformat(now)+timedelta(hours=1)).isoformat()
        with connection(self.settings) as c:
            c.execute("""CREATE TABLE rejection_conversations (
                approval_request_id INTEGER PRIMARY KEY REFERENCES approval_requests(id),
                phone TEXT NOT NULL, phase TEXT NOT NULL, mode TEXT,
                expires_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            c.execute("INSERT INTO rejection_conversations VALUES (?,?,'reason','correction',?,?)",
                (task['id'],phone or self.phone,expires,now))
        return expires

    def test_fresh_schema_uses_existing_approval_requests_only(self):
        with connection(self.settings) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rejection_conversations'").fetchone())

    def test_legacy_conversation_is_preserved_and_can_finish(self):
        task=self.new_request()
        expires=self.legacy_conversation(task)
        migrate(self.settings)
        migrate(self.settings)
        with connection(self.settings) as c:
            row=c.execute('SELECT * FROM approval_requests WHERE id=?',(task['id'],)).fetchone()
            self.assertEqual(row['rejection_phase'],'reason')
            self.assertEqual(row['rejection_mode'],'correction')
            self.assertEqual(row['rejection_expires_at'],expires)
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='rejection_conversations'").fetchone())
        self.send(task,'conversation','Correct quantities','migrated-reason')
        self.assertEqual(self.callbacks()[0]['rejection']['mode'],'correction')

    def test_migration_does_not_overwrite_newer_conversation_state(self):
        task=self.new_request()
        self.legacy_conversation(task)
        with connection(self.settings) as c:
            c.execute("UPDATE approval_requests SET rejection_phase='closed',rejection_updated_at='2099-01-01T00:00:00+00:00' WHERE id=?",(task['id'],))
        migrate(self.settings)
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT rejection_phase FROM approval_requests WHERE id=?',(task['id'],)).fetchone()[0],'closed')

    def test_mismatched_legacy_actor_rolls_back_without_losing_data(self):
        task=self.new_request()
        self.legacy_conversation(task,phone='255700000001')
        with self.assertRaises(RuntimeError):
            migrate(self.settings)
        with connection(self.settings) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM rejection_conversations').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT rejection_phase FROM approval_requests WHERE id=?',(task['id'],)).fetchone()[0],'closed')
