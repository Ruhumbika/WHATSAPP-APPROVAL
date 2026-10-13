"""Button/list navigation and context-aware help using the durable inbox queue."""
from datetime import datetime, timedelta, timezone
import json
import re
from .db import connection, utc_now
from .integrations.permissions import WorkflowDenied
from . import admin_controls as controls

GUIDE = {
    'pending_help': ('Pending approvals', 'Choose Pending approvals to see requests currently assigned to you. Select a request to review its summary and PDF. Type PENDING for a shortcut.'),
    'find_help': ('Find a request', 'Choose Find a request, then enter REQ 312 or PA 108 using the actual request ID. You can only view requests you are permitted to access.'),
    'decision_help': ('Approve or reject', 'Read the PDF before choosing Approve or Reject. Reject asks whether to return for correction or reject completely, then asks for a reason. Sending the reason submits that rejection. CANCEL keeps the request pending.'),
    'progress_help': ('Track progress', 'Use View progress on a notification, or type STATUS REQ 312 / STATUS PA 108. Completed documents are sent after the full approval workflow finishes.'),
}


def init_schema(conn):
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(inbox_replies)')}
    if 'actions_json' not in columns:
        conn.execute('ALTER TABLE inbox_replies ADD COLUMN actions_json TEXT')
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_sessions (
        phone TEXT PRIMARY KEY, state_json TEXT NOT NULL, expires_at TEXT NOT NULL)''')
    controls.init_schema(conn)


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
            AND rejection_phase IN ('choice','reason') AND rejection_expires_at>? AND expires_at>? LIMIT 2''', (phone, utc_now(), utc_now()))]
    return rows


def handle(settings, response):
    from .inbox import enqueue_reply, parse
    from .progress import COMMAND as PROGRESS
    from .service import ServiceError
    action = response.get('action')
    text = response.get('text', '').strip()
    upper = text.upper()
    command = None
    if action == 'menu':
        with connection(settings) as conn:
            parent = conn.execute("SELECT * FROM inbox_replies WHERE token=? AND phone=? AND message_id=? AND status IN ('sent','simulated') AND expires_at>?",
                (response.get('reference'), response.get('phone'), response.get('context_id'), utc_now())).fetchone()
        if parent is None:
            raise ServiceError('Menu expired or unavailable. Send MENU for a fresh menu.', 403)
        options = json.loads(parent['actions_json'] or '[]')
        index = response.get('menu_index')
        if type(index) is not int or not 0 <= index < len(options):
            raise ServiceError('Invalid menu selection.', 403)
        command = options[index]['command']
        # All admin selections must be authorized again, even from an old menu.
        if command.get('ui', '').startswith('admin') and not controls.is_admin(settings, response['phone']):
            command = ui('denied')
    elif action not in ('conversation', None):
        return None
    elif upper in ('HELP', 'GUIDE', 'USER MANUAL', 'MANUAL'):
        command = ui('guide')
    elif upper in ('MENU', 'HI', 'HELLO', 'START'):
        command = ui('main')
    elif upper == 'ADMIN':
        command = ui('admin') if controls.is_admin(settings, response['phone']) else ui('denied')
    elif parse(text) is not None or PROGRESS.fullmatch(text):
        return None
    else:
        active = rejection(settings, response['phone'])
        # Requested free-text reasons belong to the existing rejection state machine.
        if active:
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
        state = session(settings, response['phone'])
        if upper == 'CANCEL':
            command = ui('main')
        elif state and state['ui'] == 'find':
            command = ui('find', invalid=True)
        elif state and state['ui'] == 'admin_text':
            if not controls.is_admin(settings, response['phone']):
                command = ui('denied')
            elif 3 <= len(text) <= 300:
                command = ui('admin_propose', key=state['key'], value=text)
            else:
                command = ui('admin_text', key=state['key'], invalid=True)
        else:
            command = ui('main')
    active = rejection(settings, response['phone'])
    if active and command.get('ui') not in ('guide', 'resume', 'leave', 'exit', 'denied', *GUIDE):
        command = ui('exit')
    if command.get('ui') == 'guide' and active:
        command = ui('context_help')
    if command.get('ui') == 'leave':
        # Cancellation runs normal task/actor/ERP checks; never abandon all tasks.
        if len(active) != 1 or active[0]['gateway_reference_id'] != command.get('task'):
            command = ui('main')
        else:
            from .service import decide
            return decide(settings, dict(response, action='conversation', text='CANCEL',
                reference=active[0]['gateway_reference_id'], context_id=None))
    return enqueue_reply(settings, response, command)


def prepare(settings, job, command):
    name, phone = command['ui'], job['phone']
    back = ('Main menu', ui('main'))
    if name.startswith('admin'):
        controls.require_admin(settings, phone)
    if name == 'denied':
        return page('Admin access is not enabled for this number.', back, ('User guide', ui('guide')))
    if name == 'main':
        set_session(settings, phone, None)
        _, values = controls.read(settings)
        return page('*PETA APPROVAL ASSISTANT*\n' + values['welcome'],
                    ('Pending approvals', {'kind': None, 'reference': None, 'company': None, 'cursor': 0}),
                    ('Find a request', ui('find')), ('Help & more', ui('more')))
    if name == 'more':
        options = [('User guide', ui('guide')), ('Contact support', ui('support')), back]
        if controls.is_admin(settings, phone):
            options.insert(0, ('Admin controls', ui('admin')))
        return page('*HELP & MORE*\nChoose an option.', *options)
    if name == 'guide':
        return page('*USER GUIDE*\nChoose a topic. HELP is available at any time.',
                    *((title, ui(key)) for key, (title, _) in GUIDE.items()), back)
    if name in GUIDE:
        title, body = GUIDE[name]
        return page('*' + title.upper() + '*\n' + body, ('User guide', ui('guide')), back)
    if name == 'support':
        _, values = controls.read(settings)
        return page('*SUPPORT*\n' + values['support'], back)
    if name == 'find':
        set_session(settings, phone, ui('find'))
        body = 'Enter a request reference, for example REQ 312 or PA 108.\nUse the actual request ID.'
        if command.get('invalid'): body = 'That reference was not recognized.\n\n' + body
        return page('*FIND A REQUEST*\n' + body, ('User guide', ui('find_help')), back)
    if name in ('exit', 'context_help', 'resume'):
        active = rejection(settings, phone)
        if len(active) != 1:
            return page('More than one rejection may be open. Reply to the specific rejection prompt to continue.', ('User guide', ui('guide')))
        task = active[0]
        command['_rejection_reference'] = task['gateway_reference_id']
        with connection(settings) as conn:
            conn.execute('UPDATE inbox_replies SET command_json=? WHERE id=?',
                         (json.dumps(command), job['id']))
        label = ('REQ ' if task['request_type'] == 'requisition_approval' else 'PA ') + task['source_reference_id']
        if name == 'exit':
            return page(f'*{label}*\nA rejection is in progress. Continue it or cancel the rejection?',
                        ('Continue rejection', ui('resume')), ('Cancel rejection', ui('leave', task=task['gateway_reference_id'])), ('Help', ui('guide')))
        if task['rejection_phase'] == 'reason':
            body = f'*{label} — REASON REQUIRED*\nWrite a reason of 3–2000 characters. Sending the reason submits your rejection.\nHELP shows guidance. CANCEL keeps the request pending.'
            return page(body, ('Cancel rejection', ui('leave', task=task['gateway_reference_id'])))
        return page(f'*{label} — REJECTION OPTIONS*\nReturn to the original rejection prompt and select Return to edit, Reject completely or Cancel. You can also reply 1, 2 or 3 to that prompt.',
                    ('Continue rejection', ui('resume')), ('Cancel rejection', ui('leave', task=task['gateway_reference_id'])))
    revision, values = controls.read(settings)
    admin_back = ('Admin menu', ui('admin'))
    if name == 'admin':
        set_session(settings, phone, None)
        return page('*GATEWAY ADMIN*\nScope: all gateway companies\nChoose a control.',
                    ('Reminder settings', ui('admin_reminders')), ('Queue status', ui('admin_status')),
                    ('Help content', ui('admin_content')), ('Recent changes', ui('admin_audit')), back)
    if name == 'admin_reminders':
        state = 'Paused' if values['reminders_paused'] else ('Off (limit 0)' if not values['reminder_limit'] else 'On')
        body = f"*REMINDER SETTINGS*\nScope: all gateway companies\nReminders: {state}\nInterval: {values['reminder_hours']} hours\nMaximum: {values['reminder_limit']} per stage\nAutomatic notifications: {'On' if settings.workflow_notifications_enabled else 'Off in environment'}"
        return page(body, ('Change interval', ui('admin_interval')), ('Change maximum', ui('admin_limit')),
                    ('Resume reminders' if values['reminders_paused'] else 'Pause reminders', ui('admin_propose', key='reminders_paused', value=not values['reminders_paused'])), admin_back)
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
    if name == 'admin_status':
        return page(controls.queue_status(settings, phone), admin_back)
    if name == 'admin_audit':
        with connection(settings) as conn:
            rows = conn.execute('SELECT setting_key,actor_phone,created_at FROM gateway_admin_audit ORDER BY id DESC LIMIT 5').fetchall()
        body = '*RECENT ADMIN CHANGES*\n' + ('\n'.join(f"{r['created_at'][:16]} UTC · {r['setting_key']} · …{r['actor_phone'][-4:]}" for r in rows) or 'No changes recorded.')
        return page(body, admin_back)
    raise WorkflowDenied('Menu unavailable. Send MENU to start again.')
