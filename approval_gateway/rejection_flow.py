"""Optional static Meta Flow: collect a draft, never commit an ERP decision."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .db import connection, utc_now
from .integrations.permissions import WorkflowDenied


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS rejection_flows (
        token TEXT PRIMARY KEY, inbox_id INTEGER NOT NULL UNIQUE,
        task_id INTEGER NOT NULL, phone TEXT NOT NULL, fingerprint TEXT NOT NULL,
        expires_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open')''')
    conn.execute('CREATE INDEX IF NOT EXISTS rejection_flows_task ON rejection_flows(task_id, state)')


def fingerprint(task):
    fields = ('request_hash', 'rejection_phase', 'rejection_mode', 'rejection_draft',
              'rejection_review_token', 'rejection_expires_at')
    return hashlib.sha256(json.dumps([task[k] for k in fields]).encode()).hexdigest()


def prepare(settings, job, task):
    from .chat_menu import task_label
    if not settings.whatsapp_rejection_flow_enabled:
        raise WorkflowDenied('The form is unavailable. Send HELP and choose Continue rejection.')
    now = utc_now()
    with connection(settings) as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = dict(conn.execute('SELECT * FROM approval_requests WHERE id=?', (task['id'],)).fetchone())
        if (current['status'] != 'pending' or current['expires_at'] <= now
                or current['rejection_phase'] == 'closed' or not current['rejection_expires_at']
                or current['rejection_expires_at'] <= now or fingerprint(current) != fingerprint(task)):
            raise WorkflowDenied('The request changed. Send HELP to resume the current task.')
        old = conn.execute('SELECT * FROM rejection_flows WHERE inbox_id=?', (job['id'],)).fetchone()
        if old:
            if old['state'] != 'open' or old['expires_at'] <= now or old['fingerprint'] != fingerprint(current):
                raise WorkflowDenied('This form expired. Send HELP to open a new form.')
            token = old['token']
        else:
            conn.execute("UPDATE rejection_flows SET state='invalidated' WHERE task_id=? AND state='open'", (task['id'],))
            token = 'RFL-' + uuid4().hex.upper()
            expires = min(job['expires_at'], current['expires_at'], current['rejection_expires_at'],
                          (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat())
            conn.execute('INSERT INTO rejection_flows VALUES(?,?,?,?,?,?,?)',
                         (token, job['id'], task['id'], job['phone'], fingerprint(current), expires, 'open'))
    label = task_label(task)[:120]
    body = (f'*{label} — REJECTION FORM*\nChoose an action and explain why. '
            'Review rejection saves a draft; confirm it in chat before anything is submitted.\n\n'
            'Form expires in 15 minutes. If it cannot open, send HELP and choose Continue rejection.')
    return body, {'flow_token': token, 'request_label': label}, task['id']


def handle(settings, response):
    if response.get('action') != 'flow_reply':
        return None
    from .service import ServiceError, _phone, _text, _preflight_decision, _check_actor
    from .rejection import _prompt, review_body
    if not settings.whatsapp_rejection_flow_enabled:
        raise ServiceError('Form unavailable. Resume using HELP.', 409)
    phone = _phone(response.get('phone'))
    message_id = _text(response.get('message_id'), 'message_id', 512)
    data = response.get('flow_data')
    if not isinstance(data, dict):
        raise ServiceError('Invalid form response. Resume using HELP.', 400)
    token, mode, reason = data.get('flow_token'), data.get('mode'), data.get('reason')
    if (not isinstance(token, str) or not re.fullmatch(r'RFL-[A-F0-9]{32}', token)
            or mode not in ('correction', 'final') or not isinstance(reason, str)
            or not 3 <= len(reason.strip()) <= 2000):
        raise ServiceError('Invalid form fields. Resume using HELP.', 400)
    with connection(settings) as conn:
        if conn.execute('SELECT 1 FROM inbound_messages WHERE message_id=?', (message_id,)).fetchone():
            return {'processed': False, 'reason': 'duplicate_message'}
        launch = conn.execute('SELECT * FROM rejection_flows WHERE token=? AND phone=?', (token, phone)).fetchone()
        if not launch:
            raise ServiceError('Form session unavailable.', 403)
        task = dict(conn.execute('SELECT * FROM approval_requests WHERE id=?', (launch['task_id'],)).fetchone())
    normalized = dict(response, action='rejected', reference=task['gateway_reference_id'], context_id=None)
    preflight = _preflight_decision(settings, normalized, phone)
    with connection(settings) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM inbound_messages WHERE message_id=?', (message_id,)).fetchone():
            return {'processed': False, 'reason': 'duplicate_message'}
        launch = conn.execute('SELECT * FROM rejection_flows WHERE token=? AND phone=?', (token, phone)).fetchone()
        parent = conn.execute('SELECT * FROM inbox_replies WHERE id=?', (launch['inbox_id'],)).fetchone()
        current = dict(conn.execute('SELECT * FROM approval_requests WHERE id=?', (launch['task_id'],)).fetchone())
        now = utc_now()
        if (launch['state'] != 'open' or launch['expires_at'] <= now
                or parent['status'] not in ('sent', 'simulated') or parent['phone'] != phone
                or not parent['message_id'] or response.get('context_id') != parent['message_id']
                or current['status'] != 'pending' or current['expires_at'] <= now
                or current['actor_phone'] != phone or current['rejection_phase'] not in ('choice', 'reason')
                or not current['rejection_expires_at'] or current['rejection_expires_at'] <= now
                or launch['fingerprint'] != fingerprint(current)
                or preflight != (current['id'], current['request_hash'])):
            raise ServiceError('Form expired or request changed. Resume using HELP.', 409)
        _check_actor(conn, current, phone)
        review_token = 'RJC-' + uuid4().hex.upper()
        conn.execute('''UPDATE approval_requests SET rejection_phase='reason', rejection_mode=?,
            rejection_draft=?, rejection_review_token=?, rejection_paused=0, rejection_updated_at=? WHERE id=?''',
            (mode, reason.strip(), review_token, now, current['id']))
        conn.execute('INSERT INTO inbound_messages VALUES (?,?)', (message_id, now))
        _prompt(conn, current, response, review_body(dict(current, rejection_mode=mode, rejection_draft=reason.strip())), now, review_token)
        conn.execute("UPDATE rejection_flows SET state='consumed' WHERE token=?", (token,))
    return {'processed': True, 'reason': 'rejection_draft_saved'}
