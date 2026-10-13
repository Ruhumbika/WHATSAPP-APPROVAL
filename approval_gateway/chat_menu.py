"""Button/list navigation and context-aware help using the durable inbox queue."""
from datetime import datetime, timedelta, timezone
import json
import re
from .db import connection, utc_now
from .integrations.permissions import WorkflowDenied, IntegrationUnavailable
from . import admin_controls as controls

GUIDE = {
    'mine_help': ('My requests', 'Choose My requests to see requests you created, their status and current reviewers. Choose a company if prompted. Shortcut: MY REQUESTS OLD or MY REQUESTS NEW. Availability requires an active ERP identity binding.'),
    'pending_help': ('Pending approvals', 'Choose Pending approvals, then Oldest first or Newest first (by request creation date). Select a request for its summary and PDF. Back to pending keeps your order and page; Change order starts again. Shortcuts: PENDING OLD or PENDING NEW.'),
    'find_help': ('Find a request', 'Choose Find a request, then enter REQ 312 or PA 108 using the actual request ID. You can only view requests you are permitted to access.'),
    'decision_help': ('Approve or reject', 'Read the PDF before choosing Approve or Reject. Reject asks whether to return for correction or reject completely, then asks for a reason. Your reason is saved as a draft. Review it, then press Submit rejection. MENU pauses and saves your place; Cancel rejection keeps the request pending.'),
    'progress_help': ('Track progress', 'Use View progress on a notification, or type STATUS REQ 312 / STATUS PA 108. Completed documents are sent after the full approval workflow finishes.'),
}


def init_schema(conn):
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(inbox_replies)')}
    if 'actions_json' not in columns:
        conn.execute('ALTER TABLE inbox_replies ADD COLUMN actions_json TEXT')
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_sessions (
        phone TEXT PRIMARY KEY, state_json TEXT NOT NULL, expires_at TEXT NOT NULL)''')
    controls.init_schema(conn)
    from .service_context import init_schema as init_services
    init_services(conn)


def page(body, *options):
    # Each option is (label, command). Payloads contain only an opaque job token/index.
    return body, {'menu_options': [{'title': title, 'command': command} for title, command in options]}, None


def ui(name, **values):
    return dict(ui=name, **values)


def session(settings, phone):
    with connection(settings) as conn:
        row = conn.execute('SELECT state_json FROM chat_sessions WHERE phone=? AND expires_at>?', (phone, utc_now())).fetchone()
    return json.loads(row[0]) if row else None


def set_session(settings, phone, state):
    with connection(settings) as conn:
        if state is None:
            conn.execute('DELETE FROM chat_sessions WHERE phone=?', (phone,))
        else:
            expires = (datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat()
            conn.execute('INSERT INTO chat_sessions VALUES(?,?,?) ON CONFLICT(phone) DO UPDATE SET state_json=excluded.state_json,expires_at=excluded.expires_at',
                         (phone, json.dumps(state), expires))


def rejection(settings, phone):
    with connection(settings) as conn:
        rows = [dict(r) for r in conn.execute('''SELECT * FROM approval_requests WHERE actor_phone=? AND status='pending'
            AND rejection_phase IN ('choice','reason') AND rejection_expires_at>? AND expires_at>? ORDER BY id DESC LIMIT 100''', (phone, utc_now(), utc_now()))]
    return rows


def pause(settings, phone):
    with connection(settings) as conn:
        conn.execute("UPDATE approval_requests SET rejection_paused=1 WHERE actor_phone=? AND status='pending' AND rejection_phase!='closed'", (phone,))


def handle(settings, response):
    from .inbox import enqueue_reply, parse
    from .progress import COMMAND as PROGRESS
    from .service import ServiceError
    action = response.get('action')
    text = response.get('text', '').strip()
    upper = text.upper()
    active = rejection(settings, response['phone'])
    if response.get('review_action'):
        return None
    if action == 'menu':
        with connection(settings) as conn:
            parent = conn.execute("SELECT * FROM inbox_replies WHERE token=? AND phone=? AND message_id=? AND status IN ('sent','simulated') AND expires_at>?",
                (response.get('reference'), response.get('phone'), response.get('context_id'), utc_now())).fetchone()
        if parent is None:
            return enqueue_reply(settings, response, ui('recovery', reference=response.get('reference')))
        options = json.loads(parent['actions_json'] or '[]')
        index = response.get('menu_index')
        if type(index) is not int or not 0 <= index < len(options):
            return enqueue_reply(settings, response, ui('recovery', reference=response.get('reference')))
        command = options[index]['command']
        if command.get('ui', '').startswith('admin') and not controls.is_admin(settings, response['phone']):
            command = ui('denied')
    elif action not in ('conversation', None):
        return None
    elif upper in ('HELP', 'GUIDE', 'USER MANUAL', 'MANUAL'):
        command = ui('context_help') if any(not t['rejection_paused'] for t in active) else ui('guide')
    elif upper in ('MENU', 'HI', 'HEY', 'HELLO', 'START'):
        command = ui('exit') if active and upper != 'MENU' else ui('main')
    elif upper == 'ADMIN':
        command = ui('admin') if controls.is_admin(settings, response['phone']) else ui('denied')
    elif parse(text) is not None or PROGRESS.fullmatch(text):
        pause(settings, response['phone'])
        return None
    else:
        state = session(settings, response['phone'])
        if state and state['ui'] == 'find':
            command = ui('main') if upper == 'CANCEL' else ui('find', invalid=True)
        elif state and state['ui'] == 'admin_time':
            if upper == 'CANCEL':
                command = ui('admin_quiet')
            elif not controls.is_admin(settings, response['phone']):
                command = ui('denied')
            else:
                try:
                    controls.validate(state['key'], text)
                    command = ui('admin_propose', key=state['key'], value=text)
                except WorkflowDenied:
                    command = ui('admin_time', key=state['key'])
        elif state and state['ui'] == 'admin_text':
            if upper == 'CANCEL':
                command = ui('admin')
            elif not controls.is_admin(settings, response['phone']):
                command = ui('denied')
            elif 3 <= len(text) <= 300:
                command = ui('admin_propose', key=state['key'], value=text)
            else:
                command = ui('admin_text', key=state['key'], invalid=True)
        elif response.get('reference') or response.get('context_id') or any(not t['rejection_paused'] for t in active):
            # Direct task replies still use the original actor and workflow checks.
            if response.get('context_id') and not response.get('reference'):
                with connection(settings) as conn:
                    prompt = conn.execute("""SELECT command_json FROM inbox_replies
                        WHERE phone=? AND message_id=? AND status IN ('sent','simulated') AND expires_at>?""",
                        (response['phone'], response['context_id'], utc_now())).fetchone()
                reference = json.loads(prompt[0]).get('_rejection_reference') if prompt else None
                if reference and any(t['gateway_reference_id'] == reference for t in active):
                    response['reference'] = reference
                    response['context_id'] = None
            return None
        else:
            command = ui('main')
    if command.get('ui') not in ('resume', 'leave', 'context_help', 'exit', 'rejections', 'rejection_view'):
        pause(settings, response['phone'])
    return enqueue_reply(settings, response, command)


def verified_task(settings, phone, reference):
    from .service import _preflight_decision, ServiceError
    with connection(settings) as conn:
        row = conn.execute("SELECT * FROM approval_requests WHERE gateway_reference_id=? AND actor_phone=?", (reference, phone)).fetchone()
    if not row or row['status'] != 'pending' or row['expires_at'] <= utc_now() or row['rejection_phase'] == 'closed' or not row['rejection_expires_at'] or row['rejection_expires_at'] <= utc_now():
        raise WorkflowDenied('This rejection is no longer available. Open Pending approvals for current requests.')
    task = dict(row)
    try:
        _preflight_decision(settings, {'reference': reference, 'context_id': None, 'phone': phone, 'action': 'rejected', 'message_id': 'resume-check-' + reference}, phone)
    except ServiceError as exc:
        if exc.status >= 500:
            from .integrations.permissions import IntegrationUnavailable
            raise IntegrationUnavailable('Workflow verification unavailable') from exc
        raise WorkflowDenied('The request or your assignment changed. Open Pending approvals.') from exc
    return task


def task_label(task):
    prefix = 'REQ' if task['request_type'] == 'requisition_approval' else 'PA'
    reference = task['source_reference_id']
    return reference if reference.upper().startswith(prefix) else f'{prefix} {reference}'


def task_summary(task):
    from .inbox import clean
    payload = json.loads(task['payload_json'])
    snapshot = payload.get('record_snapshot') or {}
    purpose = payload.get('details') or snapshot.get('remarks') or 'See request PDF'
    amount = payload.get('total_display') or snapshot.get('total_display') or payload.get('amount')
    if not amount and snapshot.get('items'):
        try:
            from .integrations.peta import _detailed_message_fields
            amount = _detailed_message_fields(snapshot)['total_display']
        except (ValueError, KeyError, TypeError, IntegrationUnavailable):
            pass
    mode = {'correction': 'Return for correction', 'final': 'Reject completely'}.get(task['rejection_mode'], 'Not chosen yet')
    return f"*{task_label(task)}*\n{clean(purpose, 120)}\nAmount: {clean(amount, 100) or 'See request PDF'}\nRejection type: {mode}"

def prepare(settings, job, command):
    name, phone = command['ui'], job['phone']
    back = ('Main menu', ui('main'))
    if name.startswith('admin'):
        controls.require_admin(settings, phone)
    if name == 'denied':
        return page('Admin access is not enabled for this number.', back, ('User guide', ui('guide')))
    if name in ('services', 'select_service'):
        from .service_context import discover, select
        if name == 'select_service':
            select(settings, phone, command['system'], command['service_id'])
            return prepare(settings, job, ui('main'))
        services, unavailable = discover(settings, phone)
        options = [((str(s['id']) + ' · ' + s['name'])[:20], ui('select_service', system=s['system'], service_id=s['id'])) for s in services[:8]]
        return page('Choose your ERP service.' if options else 'Service identity could not be verified. Please try again or contact support.', *options, back)
    if name == 'main':
        pause(settings, phone)
        set_session(settings, phone, None)
        _, values = controls.read(settings)
        options = [('Pending approvals', {'kind': None, 'reference': None, 'company': None, 'cursor': 0}),
                   ('My requests', {'kind': None, 'reference': None, 'company': None, 'cursor': 0, 'scope':'mine'}),
                   ('Find a request', ui('find')), ('User guide', ui('guide')), ('Contact support', ui('support'))]
        active = rejection(settings, phone)
        if active:
            options.insert(0, ('Resume rejection', ui('rejections')))
        if controls.is_admin(settings, phone):
            options.append(('Admin controls', ui('admin')))
        from .service_context import discover, selected, GENERIC
        services, unavailable = discover(settings, phone)
        service = selected(settings, phone, services)
        title = service['name'] + ' ERP ASSISTANT' if service else GENERIC
        if len(services) > 1:
            options.insert(0, ('Switch service' if service else 'Choose service', ui('services')))
            if not service:
                options = [o for o in options if o[0] not in ('Pending approvals', 'My requests', 'Find a request')]
        body = '*' + title + '*\n' + values['welcome']
        if unavailable and not service:
            body += '\nService verification is temporarily unavailable.'
        # Only supported inbox adapters are shown, never send another ERP's command to PETA.
        if service and service['system'] != 'peta':
            options = [o for o in options if o[0] not in ('Pending approvals', 'My requests', 'Find a request')]
            body += '\nRequest browsing for this service is not configured yet.'

        if active:
            body += '\nYour unfinished rejection is saved. Choose Resume rejection to continue.'
        return page(body, *options)
    if name == 'recovery':
        with connection(settings) as conn:
            task = conn.execute('SELECT * FROM approval_requests WHERE gateway_reference_id=? AND actor_phone=?',
                                (command.get('reference'), phone)).fetchone()
            if task is None:
                task = conn.execute('''SELECT r.* FROM inbox_replies i JOIN approval_requests r ON r.id=i.task_id
                    WHERE i.token=? AND i.phone=? AND r.actor_phone=?''', (command.get('reference'), phone, phone)).fetchone()
            if task is None and command.get('context_id'):
                task = conn.execute("""SELECT r.* FROM decision_repeat_notifications n
                    JOIN approval_requests r ON r.id=n.approval_request_id
                    WHERE n.message_id=? AND n.phone=? AND r.actor_phone=? LIMIT 1""",
                    (command['context_id'], phone, phone)).fetchone()
        body = 'That action is expired or unavailable. Open your current requests or resume an unfinished rejection.'
        if task and task['retirement_reason']:
            body = task_label(dict(task)) + ': your earlier task is no longer current.'
            if task['retirement_had_draft']:
                body += ' Your unfinished rejection was closed without submission.'
        return page(body,
                    ('Pending approvals', {'kind': None, 'reference': None, 'company': None, 'cursor': 0}),
                    ('Resume rejection', ui('rejections')), back)
    if name == 'more':
        options = [('User guide', ui('guide')), ('Contact support', ui('support')), back]
        if controls.is_admin(settings, phone):
            options.insert(0, ('Admin controls', ui('admin')))
        return page('*HELP & MORE*\nChoose an option.', *options)
    if name == 'guide':
        options = [(title, ui(key)) for key, (title, _) in GUIDE.items()]
        options.append(('Resume & drafts', ui('resume_help')))
        if controls.is_admin(settings, phone):
            options.append(('Admin controls', ui('admin_help')))
        return page('*USER GUIDE*\nChoose a topic. HELP is available at any time.', *options, back)
    if name == 'resume_help':
        return page('Main menu pauses without deleting your rejection draft. Choose Resume rejection to continue here. Sending a reason saves a draft; only Submit rejection sends it. Cancel rejection keeps the request pending. Drafts expire with the rejection session or task. When enabled, Open rejection form lets you choose an action and enter a reason together. Review rejection returns a draft to chat; Submit rejection is still required. If the form cannot open, use Continue rejection.',
                    ('Resume rejection', ui('rejections')), ('User guide', ui('guide')), back)
    if name == 'admin_help':
        return page('Admin controls are available from Main menu or the ADMIN shortcut. Change reminders and quiet hours (ADMIN > Reminder settings > Quiet hours), welcome/support text, view queues, delivery receipts and recent changes. Settings apply across gateway companies and require confirmation. Admin access does not grant approval permissions.',
                    ('Admin controls', ui('admin')), ('User guide', ui('guide')), back)
    if name in GUIDE:
        title, body = GUIDE[name]
        return page('*' + title.upper() + '*\n' + body, ('User guide', ui('guide')), back)
    if name in ('support', 'support_service'):
        from .service_context import discover, selected, GENERIC
        from .inbox import clean
        services, unavailable = discover(settings, phone)
        service = selected(settings, phone, services)
        if name == 'support_service':
            service = next((s for s in services if s['system'] == command.get('system') and s['id'] == command.get('service_id')), None)
            if service is None:
                return page('Service access could not be verified. Please reopen Contact support.', back)
        elif len(services) > 1:
            return page('*CONTACT SUPPORT*\nWhich service do you need help with?',
                        *[((str(s['id']) + ' · ' + clean(s['name'], 17))[:24],
                           ui('support_service', system=s['system'], service_id=s['id'])) for s in services[:8]], back)
        title = clean(service['name'], 100) if service else GENERIC
        body = '*' + title + ' — SUPPORT*'
        if service:
            body += '\nContact your ' + clean(service['name'], 100) + ' administrator.\nA verified company support name/phone is not available yet.'
        elif unavailable:
            body += '\nService verification is temporarily unavailable. Please try again.'
        else:
            body += '\nWe could not identify your ERP service. Please contact your company IT support.'
        _, values = controls.read(settings)
        legacy = ('Contact your PETA system administrator for assistance.', 'Contact your ERP service administrator for assistance.')
        if values['support'].strip() not in legacy:
            body += '\n\nGroup support information:\n' + values['support']
        return page(body, back)
    if name == 'find':
        set_session(settings, phone, ui('find'))
        body = 'Enter a request reference, for example REQ 312 or PA 108.\nUse the actual request ID.'
        if command.get('invalid'): body = 'That reference was not recognized.\n\n' + body
        return page('*FIND A REQUEST*\n' + body, ('User guide', ui('find_help')), back)
    if name in ('exit', 'context_help', 'resume', 'leave', 'rejections', 'rejection_view', 'rejection_form'):
        active = rejection(settings, phone)
        reference = command.get('task')
        if not reference:
            if len(active) == 1:
                reference = active[0]['gateway_reference_id']
            elif active:
                offset = max(0, int(command.get('offset', 0)))
                options = [(task_label(t), ui('exit', task=t['gateway_reference_id'])) for t in active[offset:offset+7]]
                if offset + 7 < len(active):
                    options.append(('Next page', ui('rejections', offset=offset+7)))
                if offset:
                    options.append(('Back', ui('rejections', offset=max(0, offset-7))))
                return page('*UNFINISHED REJECTIONS*\nChoose the request to continue.', *options, back)
            else:
                return page('No active rejection draft. Open Pending approvals to review a current request.',
                            ('Pending approvals', {'kind': None, 'reference': None, 'company': None, 'cursor': 0}), back)
        try:
            task = verified_task(settings, phone, reference)
        except WorkflowDenied as exc:
            return page(str(exc), ('Pending approvals', {'kind': None, 'reference': None, 'company': None, 'cursor': 0}), back)
        command['_rejection_reference'] = reference
        with connection(settings) as conn:
            conn.execute('UPDATE inbox_replies SET command_json=? WHERE id=?', (json.dumps(command), job['id']))
        if name == 'rejection_form':
            from .rejection_flow import prepare
            return prepare(settings, job, task)
        if name == 'rejection_view':
            from .inbox import prepare as prepare_inbox
            source_id = re.sub(r'^(REQ|PA)[ -]*', '', task['source_reference_id'], flags=re.I)
            search = {'kind': 'requisitions' if task['request_type'] == 'requisition_approval' else 'payment-advices',
                      'reference': source_id, 'company': task['company'], 'cursor': 0, 'read_only': True}
            return prepare_inbox(settings, dict(job, command_json=json.dumps(search)))
        if name in ('resume', 'leave'):
            from .service import decide, ServiceError
            with connection(settings) as conn:
                conn.execute("UPDATE approval_requests SET rejection_paused=1 WHERE actor_phone=? AND status='pending'", (phone,))
                conn.execute('UPDATE approval_requests SET rejection_paused=0 WHERE id=?', (task['id'],))
            set_session(settings, phone, None)
            try:
                decide(settings, {'action': 'rejected' if name == 'resume' else 'conversation',
                    'text': 'Resume rejection' if name == 'resume' else 'CANCEL', 'phone': phone,
                    'reference': reference, 'context_id': None, 'message_id': job['inbound_id']})
            except ServiceError as exc:
                if exc.status >= 500:
                    from .integrations.permissions import IntegrationUnavailable
                    raise IntegrationUnavailable('Workflow unavailable') from exc
                return page('Request changed. Refresh your pending approvals.', back)
            # The rejection queue sends the actual current step, without a second acknowledgement.
            return '', {'delegated': True}, None
        body = task_summary(task) + '\nAn unfinished rejection is saved. No decision has been submitted.'
        if name == 'context_help':
            body += '\nContinue to choose a rejection type, enter a reason or review your saved draft. Submit rejection is the final confirmation.'
        form = [('Open rejection form', ui('rejection_form', task=reference))] if settings.whatsapp_rejection_flow_enabled else []
        return page(body, *form, ('Continue rejection', ui('resume', task=reference)),
                    ('View request', ui('rejection_view', task=reference)),
                    ('Cancel rejection', ui('leave', task=reference)), back,
                    ('User guide', ui('guide')))
    revision, values = controls.read(settings)
    admin_back = ('Admin menu', ui('admin'))
    if name == 'admin':
        set_session(settings, phone, None)
        return page('*GATEWAY ADMIN*\nScope: all gateway companies\nChoose a control.',
                    ('Reminder settings', ui('admin_reminders')), ('Queue status', ui('admin_status')), ('Delivery monitor', ui('admin_delivery')),
                    ('Help content', ui('admin_content')), ('Recent changes', ui('admin_audit')), back)
    if name == 'admin_reminders':
        state = 'Paused' if values['reminders_paused'] else ('Off (limit 0)' if not values['reminder_limit'] else 'On')
        body = f"*REMINDER SETTINGS*\nScope: all gateway companies\nReminders: {state}\nInterval: {values['reminder_hours']} hours\nMaximum: {values['reminder_limit']} per stage\nAutomatic notifications: {'On' if settings.workflow_notifications_enabled else 'Off in environment'}"
        return page(body, ('Change interval', ui('admin_interval')), ('Change maximum', ui('admin_limit')), ('Quiet hours', ui('admin_quiet')),
                    ('Resume reminders' if values['reminders_paused'] else 'Pause reminders', ui('admin_propose', key='reminders_paused', value=not values['reminders_paused'])), admin_back)
    if name == 'admin_quiet':
        body = f"*QUIET HOURS*\nScope: all gateway companies\nTimezone: Africa/Dar_es_Salaam\nStatus: {'On' if values['quiet_enabled'] else 'Off'}\nQuiet from {values['quiet_start']} until {values['quiet_end']} daily.\nOnly automatic reminders wait. Requests, decisions and replies continue normally."
        return page(body, ('Disable quiet hours' if values['quiet_enabled'] else 'Enable quiet hours', ui('admin_propose', key='quiet_enabled', value=not values['quiet_enabled'])),
            ('Set start time', ui('admin_time', key='quiet_start')), ('Set end time', ui('admin_time', key='quiet_end')), admin_back)
    if name == 'admin_time':
        if command['key'] not in ('quiet_start', 'quiet_end'): raise WorkflowDenied('Invalid time setting.')
        set_session(settings, phone, ui('admin_time', key=command['key']))
        return page('Send a time in 24-hour HH:MM format, for example 18:00 or 08:00. Timezone: Africa/Dar_es_Salaam. You will confirm before saving. Type CANCEL to exit.', admin_back)
    if name in ('admin_interval', 'admin_limit'):

        key = 'reminder_hours' if name == 'admin_interval' else 'reminder_limit'
        choices = (1, 4, 6, 12, 24) if key == 'reminder_hours' else (0, 1, 3, 5, 10)
        return page('*REMINDER SETTINGS*\nSelect a value.',
                    *((f'{value} hours' if key == 'reminder_hours' else f'{value} reminders', ui('admin_propose', key=key, value=value)) for value in choices), admin_back)
    if name == 'admin_content':
        return page('*HELP CONTENT*\nChoose the message to edit.', ('Welcome text', ui('admin_text', key='welcome')),
                    ('Support details', ui('admin_text', key='support')), admin_back)
    if name == 'admin_text':
        if command['key'] not in ('welcome', 'support'): raise WorkflowDenied('Invalid content setting.')
        set_session(settings, phone, ui('admin_text', key=command['key']))
        return page('Send the new English message (3–300 characters). You will review it before saving.\nType CANCEL to exit.', admin_back)
    if name == 'admin_propose':
        controls.validate(command['key'], command['value'])
        set_session(settings, phone, None)
        key = command['key']
        body = f"*CONFIRM CHANGE*\nScope: all gateway companies\n{key.replace('_', ' ').title()}\n\nCurrent: {values[key]}\nNew: {command['value']}"
        return page(body, ('Confirm change', ui('admin_apply', key=key, value=command['value'], revision=revision)), ('Cancel', ui('admin')))
    if name == 'admin_apply':
        result = controls.apply(settings, phone, job['inbound_id'], command['key'], command['value'], command['revision'])
        return page(result, admin_back, back)
    if name == 'admin_delivery':
        from .delivery_monitor import report
        return page(report(settings, phone), admin_back)
    if name == 'admin_status':
        return page(controls.queue_status(settings, phone), admin_back)
    if name == 'admin_audit':
        with connection(settings) as conn:
            rows = conn.execute('SELECT setting_key,actor_phone,created_at FROM gateway_admin_audit ORDER BY id DESC LIMIT 5').fetchall()
        body = '*RECENT ADMIN CHANGES*\n' + ('\n'.join(f"{r['created_at'][:16]} UTC · {r['setting_key']} · …{r['actor_phone'][-4:]}" for r in rows) or 'No changes recorded.')
        return page(body, admin_back)
    raise WorkflowDenied('Menu unavailable. Send MENU to start again.')
