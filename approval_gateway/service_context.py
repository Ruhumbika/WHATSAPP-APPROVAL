"""Discover service labels only through configured adapters and verified identities."""
import json
from .db import connection
from .integrations.registry import get_workflow_guard
from .integrations.permissions import IntegrationUnavailable, WorkflowDenied

GENERIC = 'ABOOD GROUP OF COMPANIES ERP ASSISTANT'


def init_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS chat_service_selection (phone TEXT PRIMARY KEY, system_name TEXT NOT NULL, service_id INTEGER NOT NULL)')


def discover(settings, phone):
    with connection(settings) as conn:
        rows = conn.execute("""SELECT b.*,a.phone_number,a.active AS approver_active FROM actor_bindings b
            JOIN approvers a ON a.id=b.approver_id JOIN api_clients c ON c.system_name=b.system_name
            WHERE b.active=1 AND a.active=1 AND c.active=1 ORDER BY (a.phone_number=?) DESC,b.id LIMIT 21""", (phone,)).fetchall()
    found, unavailable = {}, False
    # Stored phone is only a hint. Never trust it as identity or service membership.
    for row in rows[:20]:
        try:
            guard = get_workflow_guard(row['system_name'])
            if hasattr(guard, '_guard'):
                guard = guard._guard({'request_type': 'requisition_approval'})
            user, profile = guard.directory.get_service_details(user_uuid=row['directory_uuid'])
            if guard._international_phone(profile['phone_number']) != phone or str(guard.directory.source_user_id(user)) != row['source_user_id']:
                continue
            service = profile.get('_verified_service') or {}
            service_id = service.get('id')
            name = service.get('name')
            if type(service_id) is not int or not isinstance(name, str) or not name.strip():
                continue
            key = (row['system_name'], service_id)
            entry = found.setdefault(key, {'system': key[0], 'id': service_id, 'name': ' '.join(name.split())[:100], 'identities': set()})
            entry['identities'].add(row['directory_uuid'])
        except (IntegrationUnavailable, WorkflowDenied, AttributeError, KeyError, TypeError, ValueError):
            unavailable = True
    # Ambiguous shared phone never selects an identity.
    return [dict(system=v['system'], id=v['id'], name=v['name']) for v in found.values() if len(v['identities']) == 1], unavailable


def selected(settings, phone, services):
    with connection(settings) as conn:
        row = conn.execute('SELECT system_name,service_id FROM chat_service_selection WHERE phone=?', (phone,)).fetchone()
    if row:
        match = next((s for s in services if s['system'] == row['system_name'] and s['id'] == row['service_id']), None)
        if match:
            return match
    return services[0] if len(services) == 1 else None


def select(settings, phone, system, service_id):
    services, _ = discover(settings, phone)
    match = next((s for s in services if s['system'] == system and s['id'] == service_id), None)
    if not match:
        raise WorkflowDenied('Service access could not be verified. Open Main menu and try again.')
    with connection(settings) as conn:
        conn.execute('INSERT INTO chat_service_selection VALUES(?,?,?) ON CONFLICT(phone) DO UPDATE SET system_name=excluded.system_name,service_id=excluded.service_id', (phone, system, service_id))
    return match
