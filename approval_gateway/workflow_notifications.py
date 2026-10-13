"""Durable, ERP-authorized workflow updates, reminders and completed documents."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import tempfile
from urllib.parse import urlencode
import urllib.request
from uuid import uuid4

from .db import connection, utc_now
from .integrations.registry import get_workflow_guard
from .integrations.peta import PetaClient
from .integrations.permissions import IntegrationUnavailable, WorkflowDenied
from .progress import render_progress, template_parameters
from .whatsapp import WhatsAppError

logger = logging.getLogger(__name__)
TYPES = {'requisition_approval': 'requisitions', 'payment_advice_approval': 'payment-advices'}
MAX_ATTEMPTS = 5


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def later(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS workflow_watches (
        id INTEGER PRIMARY KEY, source_system TEXT NOT NULL, request_type TEXT NOT NULL,
        reference_id TEXT NOT NULL, company TEXT, event_id INTEGER NOT NULL,
        stage_key TEXT, stage_since TEXT, next_check_at TEXT NOT NULL, last_error TEXT,
        UNIQUE(source_system,request_type,reference_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS workflow_notifications (
        id INTEGER PRIMARY KEY, watch_id INTEGER NOT NULL REFERENCES workflow_watches(id),
        token TEXT NOT NULL UNIQUE, dedup_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
        state_key TEXT NOT NULL, recipient_json TEXT NOT NULL, phone TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
          CHECK(status IN ('pending','sending','sent','simulated','cancelled','failed','unknown')),
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
        expires_at TEXT, message_id TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS workflow_notifications_due ON workflow_notifications(status,next_attempt_at)')


def watch_event(conn, event):
    if event['request_type'] not in TYPES:
        return
    conn.execute('''INSERT INTO workflow_watches
        (source_system,request_type,reference_id,event_id,next_check_at) VALUES (?,?,?,?,?)
        ON CONFLICT(source_system,request_type,reference_id) DO UPDATE SET
          event_id=excluded.event_id,next_check_at=excluded.next_check_at''',
        (event['source_system'], event['request_type'], event['reference_id'], event['id'], utc_now()))


def context(settings, watch):
    if watch['source_system'] != 'peta' or watch['request_type'] not in TYPES or not re.fullmatch(r'[1-9][0-9]*', watch['reference_id']):
        raise WorkflowDenied('Unsupported workflow notification scope')
    with connection(settings) as conn:
        if not conn.execute('SELECT 1 FROM api_clients WHERE system_name=? AND active=1', (watch['source_system'],)).fetchone():
            raise WorkflowDenied('Source system disabled')
    guard = get_workflow_guard(watch['source_system'])
    if hasattr(guard, '_guard'):
        guard = guard._guard(watch)
    client = PetaClient(guard.peta.base_url, guard.peta.document_access_token,
        allow_local_http=guard.peta.base_url.startswith('http://'), timeout_seconds=guard.peta.timeout_seconds)
    result = client._get(f"whatsapp-approvals/notification-context/{TYPES[watch['request_type']]}/{watch['reference_id']}")
    data = result.get('data')
    try:
        if result.get('success') is not True or not isinstance(data, dict):
            raise ValueError()
        if any(data[key] != watch[key] for key in ('source_system', 'request_type', 'reference_id')):
            raise ValueError()
        if not re.fullmatch(r'[1-9][0-9]*', data['company']) or (watch.get('company') and watch['company'] != data['company']):
            raise WorkflowDenied('Workflow company changed')
        if any(not re.fullmatch(r'[a-f0-9]{64}', data[key]) for key in ('state_key', 'revision')):
            raise ValueError()
        p = data['progress']
        if p['reference_id'] != watch['reference_id'] or p['request_type'] != watch['request_type']:
            raise ValueError()
        if type(data['completed']) is not bool or not isinstance(data['creator'], dict) or type(data['creator']['id']) is not int:
            raise ValueError()
        if data['completed'] and not (p['status'] in ('Fully approved', 'Closed') and p['total_steps'] > 0 and p['remaining_steps'] == 0
            and p['current_step'] is None and len(p['levels']) == p['total_steps']
            and all(level.get('approval', {}).get('actor', {}).get('id') and level['approval']['action'] != 'REJECTED'
                    and level['approval_status'] in ('APPROVED', 'PARTIALLY_APPROVED') for level in p['levels'])):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise IntegrationUnavailable('Invalid notification context from ERP') from exc
    return guard, client, data


def stage_key(data):
    current = data['progress'].get('current_step')
    return digest([data['cycle'], current]) if current else None


def enqueue(conn, watch, kind, state, recipient, key, expires=None):
    now = utc_now()
    conn.execute('''INSERT INTO workflow_notifications
        (watch_id,token,dedup_key,kind,state_key,recipient_json,next_attempt_at,expires_at,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dedup_key) DO NOTHING''',
        (watch['id'], 'WNF-' + uuid4().hex.upper(), key, kind, state, json.dumps(recipient), now, expires, now, now))


def sync_watch(settings, watch):
    from .admin_controls import effective
    settings = effective(settings)
    _, _, data = context(settings, watch)
    stage = stage_key(data)
    now = utc_now()
    with connection(settings) as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = dict(conn.execute('SELECT * FROM workflow_watches WHERE id=?', (watch['id'],)).fetchone())
        # An event arriving during the read must get its own immediate recheck.
        if current['event_id'] != watch['event_id']:
            return
        from .reviewer_sync import reconcile
        reconcile(conn, watch, data, settings.workflow_notifications_enabled)
        conn.execute("UPDATE workflow_notifications SET status='cancelled',last_error='Stage changed' WHERE watch_id=? AND kind='reminder' AND status='pending' AND state_key!=?", (watch['id'], stage or ''))
        since = current['stage_since'] if current['stage_key'] == stage else now
        kind = 'completed' if data['completed'] else 'progress'
        key = digest([watch['id'], kind, data['cycle'], data['state_key'], data['creator']['id']])
        if settings.workflow_notifications_enabled:
            enqueue(conn, watch, kind, data['state_key'], data['creator'], key)
        if settings.workflow_notifications_enabled and stage and settings.workflow_reminder_limit:
            seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(since)).total_seconds()
            slot = int(seconds // (settings.workflow_reminder_hours * 3600))
            # Missed intervals collapse into one current reminder, never a catch-up burst.
            if slot > 0:
                slot = min(slot, settings.workflow_reminder_limit)
                for recipient in data['progress']['current_step']['approvers']:
                    key = digest([watch['id'], 'reminder', stage, slot, recipient['id']])
                    expires = (datetime.fromisoformat(since) + timedelta(hours=(slot + 1) * settings.workflow_reminder_hours)).isoformat()
                    # Collapse overnight slots for the same reviewer; morning delivery rechecks ERP.
                    conn.execute("UPDATE workflow_notifications SET status='cancelled',last_error='Superseded reminder slot' WHERE watch_id=? AND kind='reminder' AND state_key=? AND status='pending' AND recipient_json=? AND dedup_key!=?", (watch['id'], stage, json.dumps(recipient), key))
                    enqueue(conn, watch, 'reminder', stage, recipient, key, expires)
                    # Resume can revive an unsent current slot cancelled by settings.
                    conn.execute("""UPDATE workflow_notifications SET status='pending',last_error=NULL,
                        next_attempt_at=?,expires_at=?,updated_at=? WHERE dedup_key=?
                        AND status='cancelled' AND last_error='Reminder settings changed'""",
                        (now, expires, now, key))
        conn.execute('''UPDATE workflow_watches SET company=?,stage_key=?,stage_since=?,
            next_check_at=?,last_error=NULL WHERE id=?''',
            (data['company'], stage, since, later(60) if stage else later(300), watch['id']))


def resolve_phone(settings, watch, guard, data, user):
    recipient = guard.directory.resolve_recipient(user)
    phone = guard._international_phone(recipient.phone_number)
    # Existing administrative disables and identity conflicts remain authoritative.
    with connection(settings) as conn:
        bindings = conn.execute('''SELECT b.*,a.active AS approver_active FROM actor_bindings b
            JOIN approvers a ON a.id=b.approver_id WHERE b.system_name=? AND b.company=? AND b.directory_uuid=?''',
            (watch['source_system'], data['company'], recipient.directory_user.directory_uuid)).fetchall()
        for binding in bindings:
            if not binding['active'] or not binding['approver_active'] or binding['source_user_id'] != str(recipient.peta_user_id):
                raise WorkflowDenied('Recipient binding disabled or conflicting')
        conflict = conn.execute('SELECT 1 FROM actor_bindings b JOIN approvers a ON a.id=b.approver_id\n            WHERE b.system_name=? AND b.company=? AND b.active=1 AND a.active=1 AND a.phone_number=? AND b.directory_uuid<>? LIMIT 1',
            (watch['source_system'], data['company'], phone, recipient.directory_user.directory_uuid)).fetchone()
        if conflict:
            raise WorkflowDenied('Phone is bound to a different Directory identity')
    return phone, recipient


def upload_document(settings, content, filename):
    if settings.dry_run_whatsapp:
        return {'document_media_id': '1', 'document_filename': filename}
    from .upload_document import upload_pdf
    with tempfile.TemporaryDirectory(prefix='workflow-document-') as directory:
        path = Path(directory) / filename
        path.write_bytes(content)
        try:
            media_id = str(upload_pdf(settings, path))
        except RuntimeError as exc:
            raise IntegrationUnavailable('Workflow PDF upload failed') from exc
    return {'document_media_id': media_id, 'document_filename': filename}


def completed_pdf(client, watch, data):
    query = urlencode({'company': data['company'], 'revision': data['revision']})
    request = urllib.request.Request(
        f"{client.base_url}/whatsapp-approvals/completed-document/{TYPES[watch['request_type']]}/{watch['reference_id']}?{query}",
        headers={'Accept': 'application/pdf', 'Authorization': f'Bearer {client.access_token}'})
    try:
        with client.opener.open(request, timeout=client.timeout_seconds) as response:
            if response.status != 200 or response.headers.get_content_type() != 'application/pdf':
                raise ValueError('Expected PDF')
            content = response.read(20 * 1024 * 1024 + 1)
        if not content.startswith(b'%PDF-') or len(content) > 20 * 1024 * 1024:
            raise ValueError('Invalid PDF')
        return content
    except (OSError, ValueError) as exc:
        raise IntegrationUnavailable('Completed document unavailable') from exc


def prepare_send(settings, job, watch):
    guard, client, data = context(settings, watch)
    p = data['progress']
    stored = json.loads(job['recipient_json'])
    kind = job['kind']
    current = p.get('current_step')
    if kind in ('progress', 'completed'):
        if job['state_key'] != data['state_key'] or stored['id'] != data['creator']['id']:
            raise WorkflowDenied('Notification superseded by current workflow')
        user = data['creator']
        if kind == 'completed' and not data['completed']:
            raise WorkflowDenied('Workflow is not fully completed')
    elif kind == 'reminder':
        from .admin_controls import effective
        if not effective(settings).workflow_reminder_limit:
            raise WorkflowDenied('Reminders are paused or disabled')
        if not current or job['state_key'] != stage_key(data):
            raise WorkflowDenied('Reminder step is no longer pending')
        user = next((u for u in current['approvers'] if u['id'] == stored['id']), None)
        if user is None:
            raise WorkflowDenied('Reviewer is no longer assigned')
    else:
        user = next((u for u in [data['creator'], *((current or {}).get('approvers', []))] if u['id'] == stored['id']), stored)
    phone, recipient = resolve_phone(settings, watch, guard, data, user)
    if kind == 'retired':
        with connection(settings) as conn:
            task = conn.execute("SELECT * FROM approval_requests WHERE id=?", (stored['_retired_task'],)).fetchone()
        if not task or task['status'] != 'cancelled' or not task['retirement_reason']:
            raise WorkflowDenied('Task retirement no longer applies')
        if recipient.directory_user.directory_uuid != task['actor_directory_uuid'] or str(recipient.peta_user_id) != task['actor_user_id']:
            raise WorkflowDenied('Retired reviewer identity changed')
        values = template_parameters(p)
        values[1] = 'Earlier task replaced; open your current inbox' if stored.get('_replaced') else 'Your action at the earlier step is no longer required'
        if task['retirement_had_draft'] or stored.get('_had_draft'):
            values[1] += '. Your unfinished rejection was closed without submission'
        return phone, 'workflow_progress_update', values, None

    if kind.startswith('view_'):
        if phone != stored['_reply_phone']:
            raise WorkflowDenied('Reply phone no longer matches Directory')
        query = urlencode({'company_id': data['company'], 'actor_user_id': recipient.peta_user_id})
        verified = client._get(f"whatsapp-approvals/progress/{TYPES[watch['request_type']]}/{watch['reference_id']}?{query}")
        if verified.get('success') is not True or verified['data']['reference_id'] != watch['reference_id'] or verified['data']['request_type'] != watch['request_type']:
            raise WorkflowDenied('Progress access denied')
        if kind == 'view_progress' or not current:
            return phone, 'text', render_progress(verified['data']), None
    label = 'Requisition' if watch['request_type'] == 'requisition_approval' else 'Payment Advice'
    reference = f"{label} #{watch['reference_id']}"
    if kind == 'progress':
        return phone, 'workflow_progress_update', template_parameters(p), None
    if kind in ('reminder', 'view_request'):
        actor = guard.authorize({'request_type': watch['request_type'], 'reference_id': watch['reference_id'],
            'company': data['company'], 'step_id': str(current['id']), 'actor_directory_uuid': recipient.directory_user.directory_uuid})
        if actor.phone_number != phone or actor.source_user_id != str(recipient.peta_user_id):
            raise WorkflowDenied('Reviewer identity changed')
        with connection(settings) as conn:
            task = conn.execute('''SELECT * FROM approval_requests WHERE source_system=? AND request_type=?
                AND source_reference_id=? AND company=? AND actor_directory_uuid=? AND step_id=?
                AND status='pending' AND delivery_status IN ('sent','simulated') AND expires_at>?
                ORDER BY id DESC LIMIT 1''', (watch['source_system'], watch['request_type'], watch['reference_id'],
                data['company'], recipient.directory_user.directory_uuid, str(current['id']), utc_now())).fetchone()
        actionable = task is not None and json.loads(task['payload_json']).get('_gateway_workflow', {}).get('record_digest') == actor.record_digest
        if not actionable:
            if kind == 'view_request':
                return phone, 'text', render_progress(p) + '\n\nNo active WhatsApp approval request is available. Review this request in PETA.', None
            raise WorkflowDenied('No delivered, current and unexpired approval task')
        if kind == 'reminder':
            return phone, 'workflow_action_reminder', [reference, current['role']['name'], current['position']['role_name'],
                p.get('waiting_since') or 'Not recorded', data.get('deadline') or 'No deadline set'], None
        content = guard.peta.get_document(int(watch['reference_id']))
        document = upload_document(settings, content, f"request-{watch['reference_id']}.pdf")
        _, _, checked = context(settings, watch)
        if checked['revision'] != data['revision']:
            raise IntegrationUnavailable('Workflow changed while preparing PDF')
        current_phone, _ = resolve_phone(settings, watch, guard, checked, user)
        if current_phone != phone:
            raise WorkflowDenied('Reply phone changed while preparing PDF')
        caption = f"{reference}\n{current['role']['name']} — {current['position']['role_name']}\n\nReview the attached request. Use Approve or Reject on your approval request message to respond."
        return phone, 'document', caption, document
    content = completed_pdf(client, watch, data)
    document = upload_document(settings, content, f"completed-{TYPES[watch['request_type']]}-{watch['reference_id']}.pdf")
    _, _, checked = context(settings, watch)
    if checked['revision'] != data['revision'] or not checked['completed']:
        raise IntegrationUnavailable('Workflow changed while preparing completed PDF')
    phone, _ = resolve_phone(settings, watch, guard, checked, user)
    last = p.get('latest_action') or {}
    final_action = f"{last.get('action', 'Not recorded')} by {(last.get('actor') or {}).get('username', 'Actor not recorded')}"
    return phone, 'workflow_completed_document', [reference, p['record_status'], final_action, last.get('recorded_at') or 'Not recorded'], document


def send_job(settings, client, job):
    if job['kind'] == 'reminder':
        from .admin_controls import quiet_until
        until = quiet_until(settings)
        if until:
            with connection(settings) as conn:
                expiry = (datetime.fromisoformat(until) + timedelta(hours=1)).isoformat()
                conn.execute("UPDATE workflow_notifications SET next_attempt_at=?, expires_at=CASE WHEN expires_at IS NULL OR expires_at<? THEN ? ELSE expires_at END,last_error='Deferred: quiet hours' WHERE id=? AND status='pending'", (until, expiry, expiry, job['id']))
            return
    with connection(settings) as conn:
        watch = dict(conn.execute('SELECT * FROM workflow_watches WHERE id=?', (job['watch_id'],)).fetchone())
        if job['expires_at'] and job['expires_at'] <= utc_now():
            conn.execute("UPDATE workflow_notifications SET status='cancelled',last_error='Notification window expired' WHERE id=?", (job['id'],))
            return
        claimed = conn.execute("UPDATE workflow_notifications SET status='sending',attempts=attempts+1,updated_at=? WHERE id=? AND status='pending'", (utc_now(), job['id']))
        if not claimed.rowcount:
            return
    attempts = job['attempts'] + 1
    status, message_id, error, phone = 'sent', None, None, None
    dispatching = False
    try:
        phone, name, values, document = prepare_send(settings, job, watch)
        dispatching = True
        if name == 'text':
            message_id = client.send_text(phone, values)
        elif name == 'document':
            message_id = client.send_document(phone, document, values)
        else:
            message_id = client.send_workflow_template(phone, name, values, job['token'], document)
        if not isinstance(message_id, str) or not message_id:
            raise WhatsAppError('Missing message acknowledgement', delivery_unknown=True)
        if settings.dry_run_whatsapp:
            status = 'simulated'
    except WorkflowDenied as exc:
        status, error = 'cancelled', str(exc)
    except IntegrationUnavailable:
        status = 'pending' if attempts < MAX_ATTEMPTS else 'failed'
        error = 'ERP or Directory verification/document unavailable'
    except WhatsAppError as exc:
        status = 'unknown' if exc.delivery_unknown else ('pending' if exc.http_status == 429 and attempts < MAX_ATTEMPTS else 'failed')
        error = f'WhatsApp HTTP {exc.http_status}, API code {exc.api_code}'
    except Exception:
        status, error = ('unknown' if dispatching else 'failed'), 'Unexpected workflow notification failure'
        logger.exception('Workflow notification failed before/after dispatch=%s', dispatching)
    with connection(settings) as conn:
        conn.execute('''UPDATE workflow_notifications SET status=?,phone=?,message_id=?,last_error=?,next_attempt_at=?,updated_at=?
            WHERE id=? AND status='sending' ''', (status, phone, message_id, error, later(min(1800, 60 * 2 ** (attempts-1))), utc_now(), job['id']))


def process_automatic_notifications(settings, client, limit):
    # The caller holds the gateway's existing exclusive worker lock.
    with connection(settings) as conn:
        conn.execute("UPDATE workflow_notifications SET status='unknown',last_error='Interrupted send; review acceptance' WHERE status='sending'")
        watches = [dict(r) for r in conn.execute('SELECT * FROM workflow_watches WHERE next_check_at<=? ORDER BY next_check_at,id LIMIT ?', (utc_now(), limit))]
    for watch in watches:
        try:
            sync_watch(settings, watch)
        except Exception:
            logger.warning('Workflow notification context unavailable for watch %s', watch['id'])
            with connection(settings) as conn:
                conn.execute('UPDATE workflow_watches SET next_check_at=?,last_error=? WHERE id=?', (later(300), 'ERP notification context unavailable', watch['id']))
    if not settings.workflow_notifications_enabled:
        return
    with connection(settings) as conn:
        jobs = [dict(r) for r in conn.execute("SELECT * FROM workflow_notifications WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT ?", (utc_now(), limit))]
    for job in jobs:
        send_job(settings, client, job)


def handle_view(settings, response):
    from .service import ServiceError
    if not settings.workflow_notifications_enabled:
        raise ServiceError('Workflow notifications are disabled.', 403)
    try:
        received = datetime.fromtimestamp(int(response['timestamp']), timezone.utc)
        if received > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError()
        expires = (received + timedelta(hours=23, minutes=55)).isoformat()
        if expires <= utc_now():
            return {'processed': False, 'reason': 'expired_workflow_view'}
        message_id = response['message_id']
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 512:
            raise ValueError()
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ServiceError('Invalid workflow view message.', 400) from exc
    with connection(settings) as conn:
        parent = conn.execute("SELECT * FROM workflow_notifications WHERE token=? AND status IN ('sent','simulated')", (response['reference'],)).fetchone()
        if parent is None:
            raise ServiceError('Unknown workflow notification.', 403)
        parent = dict(parent)
        watch = dict(conn.execute('SELECT * FROM workflow_watches WHERE id=?', (parent['watch_id'],)).fetchone())
    try:
        guard, _, data = context(settings, watch)
        recipient = json.loads(parent['recipient_json'])
        phone, _ = resolve_phone(settings, watch, guard, data, recipient)
        if phone != response['phone']:
            raise WorkflowDenied('Workflow view sender mismatch')
    except WorkflowDenied as exc:
        raise ServiceError('Workflow view access denied.', 403) from exc
    except IntegrationUnavailable as exc:
        raise ServiceError('Workflow view unavailable.', 503) from exc
    recipient['_reply_phone'] = response['phone']
    with connection(settings) as conn:
        enqueue(conn, watch, 'view_request' if response['view'] == 'request' else 'view_progress', '', recipient,
                'view:' + message_id, expires)
    return {'processed': True, 'reason': 'workflow_view_queued'}
