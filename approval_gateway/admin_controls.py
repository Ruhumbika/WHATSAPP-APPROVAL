"""Gateway-wide operator controls. Admin authority comes only from configuration."""
from dataclasses import replace
import json
from .db import connection, utc_now
from .integrations.permissions import WorkflowDenied


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS gateway_controls (
        id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, values_json TEXT NOT NULL)''')
    conn.execute("INSERT OR IGNORE INTO gateway_controls VALUES (1,0,'{}')")
    conn.execute('''CREATE TABLE IF NOT EXISTS gateway_admin_audit (
        id INTEGER PRIMARY KEY, inbound_id TEXT NOT NULL UNIQUE, actor_phone TEXT NOT NULL,
        setting_key TEXT NOT NULL, old_json TEXT NOT NULL, new_json TEXT NOT NULL,
        revision INTEGER NOT NULL, created_at TEXT NOT NULL)''')


def is_admin(settings, phone):
    return phone in settings.whatsapp_admin_phones


def require_admin(settings, phone):
    if not is_admin(settings, phone):
        raise WorkflowDenied('Admin access is not enabled for this number.')


def read(settings):
    with connection(settings) as conn:
        row = conn.execute('SELECT revision,values_json FROM gateway_controls WHERE id=1').fetchone()
    values = {'reminders_paused': False, 'reminder_hours': settings.workflow_reminder_hours,
              'reminder_limit': settings.workflow_reminder_limit,
              'welcome': 'How can I help you?', 'support': 'Contact your ERP service administrator for assistance.'}
    values.update(json.loads(row['values_json']))
    return row['revision'], values


def effective(settings):
    _, values = read(settings)
    return replace(settings, workflow_reminder_hours=values['reminder_hours'],
                   workflow_reminder_limit=0 if values['reminders_paused'] else values['reminder_limit'])


def validate(key, value):
    if key == 'reminders_paused' and type(value) is bool:
        return
    if key == 'reminder_hours' and type(value) is int and 1 <= value <= 168:
        return
    if key == 'reminder_limit' and type(value) is int and 0 <= value <= 10:
        return
    if key in ('welcome', 'support') and isinstance(value, str) and 3 <= len(value.strip()) <= 300:
        return
    raise WorkflowDenied('Invalid admin setting. Open ADMIN and try again.')


def apply(settings, phone, inbound_id, key, value, expected_revision):
    require_admin(settings, phone)
    validate(key, value)
    with connection(settings) as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = conn.execute('SELECT 1 FROM gateway_admin_audit WHERE inbound_id=?', (inbound_id,)).fetchone()
        if previous:
            return 'This change has already been applied.'
        row = conn.execute('SELECT * FROM gateway_controls WHERE id=1').fetchone()
        if row['revision'] != expected_revision:
            raise WorkflowDenied('Settings changed since this confirmation was shown. Open ADMIN and review them again.')
        values = json.loads(row['values_json'])
        defaults = {'reminders_paused': False, 'reminder_hours': settings.workflow_reminder_hours,
                    'reminder_limit': settings.workflow_reminder_limit, 'welcome': 'How can I help you?',
                    'support': 'Contact your ERP service administrator for assistance.'}
        old = values.get(key, defaults[key])
        values[key] = value
        revision = row['revision'] + 1
        conn.execute('UPDATE gateway_controls SET revision=?,values_json=? WHERE id=1', (revision, json.dumps(values)))
        conn.execute('''INSERT INTO gateway_admin_audit(inbound_id,actor_phone,setting_key,old_json,new_json,revision,created_at)
            VALUES(?,?,?,?,?,?,?)''', (inbound_id, phone, key, json.dumps(old), json.dumps(value), revision, utc_now()))
        if key.startswith('reminder'):
            conn.execute("UPDATE workflow_notifications SET status='cancelled',last_error='Reminder settings changed' WHERE kind='reminder' AND status='pending'")
            conn.execute('UPDATE workflow_watches SET next_check_at=?', (utc_now(),))
    return 'Settings updated successfully.'


def queue_status(settings, phone):
    require_admin(settings, phone)
    lines = ['*GATEWAY QUEUES*', 'Scope: all gateway companies', 'Sent means accepted by the provider.']
    with connection(settings) as conn:
        for title, table, column in [('Approval delivery', 'approval_requests', 'delivery_status'),
                                      ('Workflow notifications', 'workflow_notifications', 'status'),
                                      ('Inbox and menu replies', 'inbox_replies', 'status')]:
            rows = conn.execute(f'SELECT {column} AS state,COUNT(*) AS total FROM {table} GROUP BY {column}').fetchall()
            lines.extend(['', title, ', '.join(f"{r['state']}: {r['total']}" for r in rows) or 'No records'])
    return '\n'.join(lines)
