"""Inbound-only approval inbox: durable replies and live ERP/Directory checks."""
from datetime import datetime, timedelta, timezone
import json
import logging
import re
from urllib.parse import urlencode
from urllib.error import HTTPError
from uuid import uuid4

from .db import connection, utc_now
from .integrations.registry import get_workflow_guard
from .integrations.peta import PetaClient
from .integrations.permissions import IntegrationUnavailable, WorkflowDenied
from .whatsapp import WhatsAppError

logger = logging.getLogger(__name__)

KINDS = {'REQ': 'requisitions', 'PA': 'payment-advices'}
TYPES = {'requisitions': 'requisition_approval', 'payment-advices': 'payment_advice_approval'}
COMMAND = re.compile(r'(?:PENDING(?:\s+C([1-9][0-9]{0,9}))?(?:\s+NEXT\s+([0-9]{1,7}))?|(REQ|PA)[ -]+0*([1-9][0-9]{0,9})(?:\s+C([1-9][0-9]{0,9}))?)', re.I)


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS inbox_replies (
        id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE, inbound_id TEXT NOT NULL UNIQUE,
        phone TEXT NOT NULL, command_json TEXT NOT NULL, binding_id INTEGER,
        task_id INTEGER, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL, expires_at TEXT NOT NULL, message_id TEXT, last_error TEXT,
        created_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS inbox_replies_due ON inbox_replies(status,next_attempt_at)')


def parse(text):
    match = COMMAND.fullmatch(text.strip())
    if not match:
        return None
    return {'kind': KINDS.get((match[3] or '').upper()), 'reference': match[4],
            'company': match[1] or match[5], 'cursor': int(match[2] or 0)}


def guard_for(kind=None):
    guard = get_workflow_guard('peta')
    if hasattr(guard, '_guard'):
        guard = guard._guard({'request_type': TYPES.get(kind, 'requisition_approval')})
    return guard


def identity(settings, phone, command, binding_id=None):
    """Directory owns current phone. Stored phone is only a lookup hint."""
    with connection(settings) as conn:
        if not conn.execute("SELECT 1 FROM api_clients WHERE system_name='peta' AND active=1").fetchone():
            raise WorkflowDenied('PETA integration is disabled.')
        rows = [dict(r) for r in conn.execute('''SELECT b.*,a.phone_number,a.active AS approver_active
            FROM actor_bindings b JOIN approvers a ON a.id=b.approver_id
            WHERE b.system_name='peta' ORDER BY b.id''')]
    if binding_id is not None:
        rows = [r for r in rows if r['id'] == binding_id]
    if command.get('company'):
        rows = [r for r in rows if r['company'] == command['company']]
    guard = guard_for(command.get('kind'))
    profiles = {}

    def verify(candidates):
        found = []
        for row in candidates:
            if not row['active'] or not row['approver_active']:
                continue
            key = row['directory_uuid']
            if key not in profiles:
                profiles[key] = guard.directory.get_service_details(user_uuid=key)
            user, profile = profiles[key]
            if str(guard.directory.source_user_id(user)) != row['source_user_id']:
                continue
            if guard._international_phone(profile['phone_number']) == phone:
                found.append(row)
        return found

    # Fast lookup is still verified against live Directory. A changed phone can
    # discover its existing binding without rewriting any sent task's recipient.
    hinted = [r for r in rows if r['phone_number'] == phone]
    matches = verify(rows if binding_id is not None else hinted)
    if not matches and binding_id is None:
        if len({r['directory_uuid'] for r in rows if r['active'] and r['approver_active']}) > 20:
            raise WorkflowDenied('Your phone mapping needs a refresh. Specify a company, for example PENDING C1, or contact support.')
        matches = verify(rows)
    if not matches:
        raise WorkflowDenied('No active PETA identity is linked to this WhatsApp number. Contact support.')
    if len({r['directory_uuid'] for r in matches}) != 1:
        raise WorkflowDenied('This phone has conflicting identities. Contact support.')
    if len(matches) != 1:
        companies = ', '.join('C' + r['company'] for r in matches)
        raise WorkflowDenied(f'Select a company: PENDING C1 or REQ 312 C1. Your companies: {companies}')
    return matches[0], guard


def read_api(guard, binding, command):
    client = PetaClient(guard.peta.base_url, guard.peta.document_access_token,
                       allow_local_http=guard.peta.base_url.startswith('http://'), timeout_seconds=guard.peta.timeout_seconds)
    query = urlencode({'actor_user_id': binding['source_user_id'], 'company_id': binding['company'], 'cursor': command['cursor']})
    path = 'whatsapp-approvals/inbox'
    if command['kind']:
        path += f"/{command['kind']}/{command['reference']}"
    try:
        result = client._get(path + '?' + query)
    except IntegrationUnavailable as exc:
        if isinstance(exc.__cause__, HTTPError) and exc.__cause__.code in (403, 404):
            raise WorkflowDenied('Request not found or access is not available.') from exc
        raise
    if result.get('success') is not True or not isinstance(result.get('data'), dict):
        raise IntegrationUnavailable('Invalid inbox response')
    data = result['data']
    if data.get('company') != binding['company']:
        raise WorkflowDenied('Inbox company mismatch')
    if command['kind']:
        if data.get('kind') != command['kind'] or data.get('reference_id') != command['reference']:
            raise WorkflowDenied('Inbox request mismatch')
        validate_item(data, binding)
    else:
        if data.get('actor_user_id') != binding['source_user_id'] or not isinstance(data.get('items'), list) or len(data['items']) > 5:
            raise IntegrationUnavailable('Invalid inbox page')
        for item in data['items']:
            validate_item(item, binding)
            if item['can_act'] is not True:
                raise IntegrationUnavailable('Inbox contains a non-actionable record')
        cursor = data.get('next_cursor')
        if cursor is not None and (type(cursor) is not int or not command['cursor'] < cursor <= 1000000):
            raise IntegrationUnavailable('Invalid inbox cursor')
    return data


def validate_item(item, binding):
    if (item.get('company') != binding['company'] or item.get('kind') not in TYPES
        or not re.fullmatch(r'[1-9][0-9]{0,9}', str(item.get('reference_id', '')))
        or type(item.get('can_act')) is not bool or not isinstance(item.get('amounts'), dict)):
        raise IntegrationUnavailable('Invalid inbox item')


def clean(value, limit=140):
    # Keep user-entered text from injecting new sections or WhatsApp markup.
    return ' '.join(str(value or '').translate(str.maketrans('', '', '*_~`')).split())[:limit]


def label(item):
    return ('REQ' if item['kind'] == 'requisitions' else 'PA') + ' ' + item['reference_id']


def amount(item):
    values = [f'{clean(k, 12)} {clean(v, 28)}' for k, v in item['amounts'].items()]
    return '; '.join(values[:3]) + ('; more in PDF' if len(values) > 3 else '') or 'Amount not recorded'


def summary(item):
    step = item.get('current_step') or {}
    return (f"*{label(item)}* · {amount(item)}\n{clean(item.get('company_name'), 60)}\n"
            f"{clean(item.get('purpose')) or 'Purpose not recorded'}\n"
            f"Requested by: {clean(item.get('requester'), 60)}\n"
            f"Status: {clean(item.get('status'), 40)} · {item['completed_steps']}/{item['total_steps']} steps\n"
            f"Current: {clean(step.get('action'), 30)} {clean(step.get('position'), 50)}".rstrip())


def render_page(data, company):
    items = data['items']
    body = '*YOUR PENDING APPROVALS*\nOldest requests first\n'
    for item in items:
        body += f"\n*{label(item)}* · {clean(amount(item), 60)}\n{clean(item.get('purpose'), 60) or 'Purpose not recorded'}\n"
    if not items:
        body += '\nNo approvals in this batch.' if data.get('next_cursor') is not None else '\nNo pending approvals found.'
    if items:
        body += f"\nOpen: {label(items[0])} C{company}"
    if data.get('next_cursor') is not None:
        body += f"\nNext: PENDING C{company} NEXT {data['next_cursor']}"
    body += f'\nRefresh: PENDING C{company}'
    return body


def handle_inbox(settings, response):
    from .service import ServiceError, decide
    if response.get('action') == 'inbox_decision':
        with connection(settings) as conn:
            row = conn.execute("SELECT * FROM inbox_replies WHERE token=? AND status IN ('sent','simulated') AND task_id IS NOT NULL",
                               (response.get('reference'),)).fetchone()
            if row is None or row['phone'] != response['phone'] or row['message_id'] != response.get('context_id'):
                raise ServiceError('Unknown inbox action.', 403)
            job = dict(row)
            task = conn.execute('SELECT * FROM approval_requests WHERE id=?', (job['task_id'],)).fetchone()
        if task is None:
            raise ServiceError('Inbox task unavailable.', 403)
        try:
            identity(settings, response['phone'], json.loads(job['command_json']), job['binding_id'])
        except WorkflowDenied as exc:
            raise ServiceError(str(exc), 403) from exc
        except IntegrationUnavailable as exc:
            raise ServiceError('Directory unavailable.', 503) from exc
        # Validated message ownership above; normal decision path still verifies the
        # original task, current ERP snapshot/assignment, expiry and duplicate action.
        return decide(settings, dict(response, action=response['decision'], reference=task['gateway_reference_id'], context_id=None))
    command = parse(response.get('text', ''))
    if command is None:
        return None
    return enqueue_reply(settings, response, command)


def enqueue_reply(settings, response, command):
    from .service import ServiceError
    try:
        received = datetime.fromtimestamp(int(response['timestamp']), timezone.utc)
        expires = received + timedelta(hours=23, minutes=55)
        if received > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError()
        message_id = response['message_id']
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 512:
            raise ValueError()
        if not re.fullmatch(r'[1-9][0-9]{7,14}', response['phone']):
            raise ValueError()
    except (KeyError, ValueError, TypeError, OverflowError, OSError) as exc:
        raise ServiceError('Invalid inbox message.', 400) from exc
    if expires <= datetime.now(timezone.utc):
        return {'processed': False, 'reason': 'expired_inbox_reply'}
    with connection(settings) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM inbox_replies WHERE inbound_id=?', (message_id,)).fetchone():
            return {'processed': False, 'reason': 'duplicate_inbox_message'}
        recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        if conn.execute('SELECT COUNT(*) FROM inbox_replies WHERE phone=? AND created_at>?', (response['phone'], recent)).fetchone()[0] >= 10:
            return {'processed': False, 'reason': 'inbox_rate_limited'}
        conn.execute('''INSERT INTO inbox_replies(token,inbound_id,phone,command_json,next_attempt_at,expires_at,created_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(inbound_id) DO NOTHING''',
            ('IBX-' + uuid4().hex.upper(), message_id, response['phone'], json.dumps(command), utc_now(), expires.isoformat(), utc_now()))
    return {'processed': True, 'reason': 'inbox_queued'}


def prepare(settings, job):
    command = json.loads(job['command_json'])
    if command.get('ui'):
        from .chat_menu import prepare as prepare_menu
        return prepare_menu(settings, job, command)
    from .service_context import discover, selected
    with connection(settings) as conn:
        selection = conn.execute('SELECT system_name FROM chat_service_selection WHERE phone=?', (job['phone'],)).fetchone()
    services, _ = discover(settings, job['phone'])
    if len(services) > 1 and selected(settings, job['phone'], services) is None:
        raise WorkflowDenied('Choose your ERP service from Main menu before opening requests.')
    if selection and selection['system_name'] != 'peta':
        raise WorkflowDenied('Switch to the appropriate ERP service before opening its requests.')
    binding, guard = identity(settings, job['phone'], command, job.get('binding_id'))
    # Pin the verified identity for retries, including phone reassignments.
    with connection(settings) as conn:
        conn.execute('UPDATE inbox_replies SET binding_id=? WHERE id=?', (binding['id'], job['id']))
    data = read_api(guard, binding, command)
    identity(settings, job['phone'], command, binding['id'])
    if not command['kind']:
        from .chat_menu import page, ui
        options = [(label(item), {'kind': item['kind'], 'reference': item['reference_id'], 'company': binding['company'], 'cursor': 0, 'preview': True}) for item in data['items']]
        if data.get('next_cursor') is not None:
            options.append(('Next page', dict(command, company=binding['company'], cursor=data['next_cursor'])))
        options.append(('Main menu', ui('main')))
        return page(render_page(data, binding['company']), *options)
    from .chat_menu import set_session
    set_session(settings, job['phone'], None)
    service = selected(settings, job['phone'], services)
    body = 'Service: ' + (clean(service['name'], 80) if service else 'PETA') + '\n' + summary(data) + '\nNeed help? Type HELP.'
    if command.get('preview'):
        from .chat_menu import page, ui
        return page(body, ('View document', dict(command, preview=False)),
                    ('Next pending request', {'kind': None, 'reference': None, 'company': binding['company'], 'cursor': 0}),
                    ('Main menu', ui('main')))

    if not data['can_act']:
        return body + '\n\nNo action is currently assigned to you.', None, None
    actor = guard.authorize({'request_type': TYPES[command['kind']], 'reference_id': command['reference'],
                             'company': binding['company'], 'step_id': str(data['current_step']['id']),
                             'actor_directory_uuid': binding['directory_uuid']})
    if actor.source_user_id != binding['source_user_id'] or actor.phone_number != job['phone']:
        raise WorkflowDenied('Reviewer identity changed. Refresh your inbox.')
    with connection(settings) as conn:
        task = conn.execute('''SELECT * FROM approval_requests WHERE source_system='peta' AND company=?
            AND request_type=? AND source_reference_id=? AND actor_directory_uuid=? AND step_id=?
            AND actor_phone=? AND status='pending' AND delivery_status IN ('sent','simulated') AND expires_at>?
            ORDER BY id DESC LIMIT 1''', (binding['company'], TYPES[command['kind']], command['reference'],
                binding['directory_uuid'], actor.step_id, job['phone'], utc_now())).fetchone()
    from .service import _workflow_evidence
    if task is None or json.loads(task['payload_json']).get('_gateway_workflow') != _workflow_evidence(actor):
        return body + '\n\nNo current WhatsApp action is available. Review this request in PETA.', None, None
    from .workflow_notifications import upload_document
    content = guard.peta.get_document(int(command['reference']))
    document = upload_document(settings, content, f"{command['kind']}-{command['reference']}.pdf")
    # Recheck live identity, assignment and snapshot after the PDF network call.
    identity(settings, job['phone'], command, binding['id'])
    checked = guard.authorize({'request_type': TYPES[command['kind']], 'reference_id': command['reference'],
        'company': binding['company'], 'step_id': actor.step_id, 'actor_directory_uuid': binding['directory_uuid']})
    if checked != actor:
        raise WorkflowDenied('Request changed while preparing its document. Search again.')
    with connection(settings) as conn:
        current = conn.execute("SELECT 1 FROM approval_requests WHERE id=? AND status='pending' AND expires_at>?",
                               (task['id'], utc_now())).fetchone()
    if not current:
        raise WorkflowDenied('This approval task is no longer pending. Refresh your inbox.')
    if command.get('read_only'):
        document['read_only'] = True
        return body + '\nViewing this PDF does not submit a decision. Send MENU to resume your rejection.', document, None
    return body + '\n\nReview the PDF before choosing an action.', document, task['id']


def process_inbox(settings, client, limit):
    # Only called under the existing exclusive worker lock.
    with connection(settings) as conn:
        conn.execute("UPDATE inbox_replies SET status='unknown',last_error='Interrupted send; review acceptance' WHERE status='sending'")
        conn.execute("UPDATE inbox_replies SET status='expired' WHERE status='pending' AND expires_at<=?", (utc_now(),))
        jobs = [dict(r) for r in conn.execute("SELECT * FROM inbox_replies WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT ?", (utc_now(), limit))]
    for job in jobs:
        if job['expires_at'] <= utc_now():
            with connection(settings) as conn:
                conn.execute("UPDATE inbox_replies SET status='expired' WHERE id=? AND status='pending'", (job['id'],))
            continue
        attempts = job['attempts'] + 1
        with connection(settings) as conn:
            conn.execute("UPDATE inbox_replies SET status='sending',attempts=? WHERE id=? AND status='pending'", (attempts, job['id']))
        status, message_id, error, task_id = 'sent', None, None, None
        dispatching = False
        if attempts == 1:
            try:
                client.send_typing(job['inbound_id'])
            except Exception:
                # A read receipt/typing failure must never block a business response.
                logger.debug('Typing indicator unavailable for inbox job %s', job['id'])
        try:
            try:
                body, document, task_id = prepare(settings, job)
            except WorkflowDenied as exc:
                body, document, task_id = str(exc), None, None
            if document and document.get('delegated'):
                status = 'routed'
            elif job['expires_at'] <= utc_now():
                status = 'expired'
            else:
                dispatching = True
                if document and 'flow_token' in document:
                    message_id = client.send_rejection_flow(job['phone'], body, document['flow_token'], document['request_label'])
                elif document and 'menu_options' in document:
                    with connection(settings) as conn:
                        conn.execute('UPDATE inbox_replies SET actions_json=? WHERE id=?', (json.dumps(document['menu_options']), job['id']))
                    message_id = client.send_menu(job['phone'], body, document['menu_options'], job['token'])
                elif document and document.get('read_only'):
                    message_id = client.send_document(job['phone'], document, body)
                else:
                    message_id = (client.send_inbox_document(job['phone'], body, document, job['token'])
                                  if document else client.send_text(job['phone'], body))
                if not isinstance(message_id, str) or not message_id:
                    message_id = None
                    raise WhatsAppError('Missing acknowledgement', delivery_unknown=True)
                status = 'simulated' if settings.dry_run_whatsapp else 'sent'
        except IntegrationUnavailable:
            status = 'pending' if attempts < 5 else 'failed'
            error = 'PETA or Directory unavailable; no unverified data sent'
        except WhatsAppError as exc:
            status = 'unknown' if exc.delivery_unknown else ('pending' if exc.http_status == 429 and attempts < 5 else 'failed')
            error = f'WhatsApp HTTP {exc.http_status}, API code {exc.api_code}'
        except Exception:
            status = 'unknown' if dispatching else 'failed'
            error = 'Unexpected inbox failure; inspect application configuration'
            logger.exception('Inbox job %s failed; dispatching=%s', job['id'], dispatching)
        with connection(settings) as conn:
            conn.execute('''UPDATE inbox_replies SET status=?,message_id=?,last_error=?,task_id=?,next_attempt_at=? WHERE id=?''',
                (status, message_id, error, task_id, (datetime.now(timezone.utc)+timedelta(seconds=30*2**(attempts-1))).isoformat(), job['id']))
