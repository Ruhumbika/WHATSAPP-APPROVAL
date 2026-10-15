"""Fresh ERP-authorized recovery. Historical task identifiers are never revived."""
import json
from uuid import uuid4
from .db import connection, utc_now
from .admin_controls import require_admin
from .integrations.registry import get_workflow_guard
from .integrations.permissions import WorkflowDenied, IntegrationUnavailable
from .service import _prepare_task, _persist_task, _workflow_evidence, audit, ServiceError


def renew_for_actor(settings, system, request_type, reference, binding, actor):
    guard = get_workflow_guard(system)
    resolver = getattr(guard, 'resolve_workflow_event', None)
    if not callable(resolver):
        return None
    candidates = resolver(dict(event_id=str(uuid4()), source_system=system,
                               request_type=request_type, reference_id=reference))
    if not isinstance(candidates, (list, tuple)) or len(candidates) > 100:
        raise IntegrationUnavailable('Invalid recovery resolution.')
    matches = []
    for candidate in candidates:
        if (str(candidate.get('company')) != binding['company']
                or candidate.get('actor_directory_uuid') != binding['directory_uuid']):
            continue
        data, verified = _prepare_task(system, candidate)
        if (data['request_type'] != request_type or data['reference_id'] != reference
                or verified != actor or data['workflow_version'] != actor.record_digest):
            raise WorkflowDenied('Request changed during recovery. Refresh your inbox.')
        matches.append((data, verified))
    if len(matches) != 1:
        raise WorkflowDenied('Current assignment could not be uniquely verified.')
    data, verified = matches[0]
    with connection(settings) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM api_clients WHERE system_name=? AND active=1', (system,)).fetchone():
            raise WorkflowDenied('Source system disabled.')
        # Recovery does not manufacture assignments for arbitrary search results.
        prior = conn.execute('''SELECT * FROM approval_requests WHERE source_system=? AND company=?
            AND request_type=? AND source_reference_id=? AND actor_directory_uuid=?
            AND step_id=? AND workflow_version=? ORDER BY id DESC LIMIT 1''',
            (system, binding['company'], request_type, reference, actor.directory_uuid,
             actor.step_id, actor.record_digest)).fetchone()
        if prior is None:
            return None
        task, created = _persist_task(conn, settings, system, data, verified, renew_expired=True)
        if created:
            audit(conn, task['id'], 'renewed_from', {'previous_task_id': prior['id'], 'trigger': 'inbox'})
        if task['status'] == 'pending':
            task['_recovery_delivery'] = created
            return task
        return None


def trace(settings, phone, system, request_type, reference):
    require_admin(settings, phone)
    with connection(settings) as conn:
        tasks = [dict(r) for r in conn.execute('''SELECT id,company,step_id,actor_user_id,status,
            delivery_status,expires_at,execution_status,delivery_error FROM approval_requests
            WHERE source_system=? AND request_type=? AND source_reference_id=? ORDER BY id DESC LIMIT 8''',
            (system, request_type, reference))]
        events = [dict(r) for r in conn.execute('''SELECT status,attempts,last_error FROM workflow_events
            WHERE source_system=? AND request_type=? AND reference_id=? ORDER BY id DESC LIMIT 3''',
            (system, request_type, reference))]
    lines = [f'*REQUEST TRACE*\nSystem: {system}\nType: {request_type}\nReference: {reference}',
             'Historical tasks (UTC expiry):']
    for t in tasks:
        lines.append(f"Task {t['id']} | C{t['company']} | step {t['step_id']} | user {t['actor_user_id']}\n"
                     f"{t['status']} | delivery {t['delivery_status']} | execution {t['execution_status']}\nExpires {t['expires_at']}")
        if t['delivery_error']: lines.append(str(t['delivery_error'])[:180])
    if not tasks: lines.append('No stored tasks.')
    for e in events: lines.append(f"Event: {e['status']} | attempts {e['attempts']} | {str(e['last_error'] or '')[:120]}")
    with connection(settings) as conn:
        callbacks = conn.execute('''SELECT c.status,c.attempts,c.last_error,a.id FROM callback_attempts c
            JOIN approval_requests a ON a.id=c.approval_request_id WHERE a.source_system=?
            AND a.request_type=? AND a.source_reference_id=? ORDER BY c.id DESC LIMIT 3''',
            (system,request_type,reference)).fetchall()
    for c in callbacks:
        lines.append(f"Callback task {c['id']}: {c['status']} | attempts {c['attempts']} | {str(c['last_error'] or '')[:100]}")
    # Resolve read-only; no task provisioning occurs on Trace or Preview.
    try:
        resolver = getattr(get_workflow_guard(system), 'resolve_workflow_event')
        candidates = resolver(dict(event_id=str(uuid4()), source_system=system,
                                   request_type=request_type, reference_id=reference))
        if not isinstance(candidates, (list, tuple)) or len(candidates) > 100:
            raise IntegrationUnavailable('Invalid candidate collection')
        current = []
        for c in candidates:
            data, actor = _prepare_task(system, c)
            if data['request_type'] != request_type or data['reference_id'] != reference:
                raise WorkflowDenied('Resolver returned a different record.')
            current.append(f"C{data['company']} step {actor.step_id} user {actor.source_user_id}")
        lines.append('ERP verified current assignments: ' + (', '.join(current) or 'None.'))
    except (WorkflowDenied, IntegrationUnavailable, AttributeError, ServiceError):
        lines.append('ERP verification unavailable or denied. Historical state is not proof of current assignment.')
    return '\n'.join(lines)[:3700]


def reconcile(settings, phone, inbound_id, system, request_type, reference):
    require_admin(settings, phone)
    from uuid import uuid5, NAMESPACE_URL
    event_id = str(uuid5(NAMESPACE_URL, 'admin-reconcile:' + inbound_id))
    envelope = dict(event_id=event_id, source_system=system, request_type=request_type, reference_id=reference)
    with connection(settings) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM api_clients WHERE system_name=? AND active=1', (system,)).fetchone():
            raise WorkflowDenied('Source system disabled.')
        now = utc_now()
        conn.execute('''INSERT OR IGNORE INTO workflow_events(source_system,event_id,request_type,reference_id,
            payload_json,next_attempt_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)''',
            (system,event_id,request_type,reference,json.dumps(envelope,sort_keys=True,separators=(',',':')),now,now,now))
        row = conn.execute('SELECT id FROM workflow_events WHERE source_system=? AND event_id=?', (system,event_id)).fetchone()
        conn.execute('''INSERT OR IGNORE INTO gateway_admin_audit(inbound_id,actor_phone,setting_key,
            old_json,new_json,revision,created_at) VALUES(?,?,?,?,?,0,?)''',
            (inbound_id,phone,'reconcile_request','null',json.dumps(envelope),now))
    return f"Reconciliation queued as event {row['id']}. No decision has been submitted. The worker will recheck ERP/Directory before creating fresh actions. Use Trace request to see the result."


def schedule_expired(conn, watch, data):
    """A current ERP watch may request one durable resolution per expired generation."""
    current = data['progress'].get('current_step')
    if not current:
        return
    eligible = {str(a['id']) for a in current.get('approvers', [])}
    expired = conn.execute('''SELECT r.* FROM approval_requests r
        WHERE source_system=? AND request_type=? AND source_reference_id=? AND company=?
        AND step_id=? AND (status='expired' OR (status='pending' AND expires_at<=?))
        AND delivery_status!='unknown'
        AND NOT EXISTS(SELECT 1 FROM approval_requests newer
          WHERE newer.source_system=r.source_system AND newer.company=r.company
            AND newer.request_type=r.request_type AND newer.source_reference_id=r.source_reference_id
            AND newer.actor_directory_uuid=r.actor_directory_uuid AND newer.step_id=r.step_id
            AND newer.workflow_version=r.workflow_version AND newer.id>r.id)
        ORDER BY r.id''',(watch['source_system'],watch['request_type'],watch['reference_id'],
                          data['company'],str(current['id']),utc_now())).fetchall()
    ids = [r['id'] for r in expired if str(r['actor_user_id']) in eligible]
    if not ids:
        return
    from uuid import uuid5, NAMESPACE_URL
    event_id = str(uuid5(NAMESPACE_URL, 'expired-tasks:' + json.dumps(ids)))
    envelope = dict(event_id=event_id,source_system=watch['source_system'],
                    request_type=watch['request_type'],reference_id=watch['reference_id'])
    now = utc_now()
    conn.execute('''INSERT OR IGNORE INTO workflow_events(source_system,event_id,request_type,reference_id,
        payload_json,next_attempt_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)''',
        (watch['source_system'],event_id,watch['request_type'],watch['reference_id'],
         json.dumps(envelope,sort_keys=True,separators=(',',':')),now,now,now))


def retry_delivery(settings, phone, inbound_id, task_id):
    require_admin(settings, phone)
    with connection(settings) as conn:
        if conn.execute('SELECT 1 FROM gateway_admin_audit WHERE inbound_id=?',(inbound_id,)).fetchone():
            return 'Retry already requested.'
    from .service import _authorize_workflow, _workflow_evidence, _assigned_actor, _check_actor, _expired
    with connection(settings) as conn:
        task = conn.execute('SELECT * FROM approval_requests WHERE id=?',(task_id,)).fetchone()
    if task is None:
        raise WorkflowDenied('Task not found.')
    task = dict(task)
    if (task['status'] != 'pending' or _expired(task['expires_at'])
            or task['delivery_status'] != 'failed' or task['whatsapp_message_id']
            or not str(task['delivery_error'] or '').startswith('WhatsApp request failed: HTTP 4')):
        raise WorkflowDenied('Only an unexpired task with confirmed Meta HTTP 4xx rejection can be retried. Unknown/sent/blocked deliveries require investigation.')
    actor = _authorize_workflow(task['source_system'], {
        'request_type':task['request_type'],'reference_id':task['source_reference_id'],
        'company':task['company'],'step_id':task['step_id'],
        'actor_directory_uuid':task['actor_directory_uuid']})
    if (actor.phone_number != task['actor_phone'] or actor.source_user_id != task['actor_user_id']
            or json.loads(task['payload_json']).get('_gateway_workflow') != _workflow_evidence(actor)):
        raise WorkflowDenied('Identity or workflow changed. Reconcile instead.')
    with connection(settings) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM gateway_admin_audit WHERE inbound_id=?',(inbound_id,)).fetchone():
            return 'Retry already requested.'
        current = conn.execute('SELECT * FROM approval_requests WHERE id=?',(task_id,)).fetchone()
        _assigned_actor(conn, task['source_system'], task['company'], actor.directory_uuid)
        _check_actor(conn, dict(current), actor.phone_number)
        updated=conn.execute("""UPDATE approval_requests SET delivery_status='pending',delivery_attempts=0,
            delivery_next_at=?,updated_at=? WHERE id=? AND status='pending' AND delivery_status='failed'
            AND delivery_error=? AND whatsapp_message_id IS NULL AND expires_at>?""",
            (utc_now(),utc_now(),task_id,task['delivery_error'],utc_now()))
        if updated.rowcount!=1:
            raise WorkflowDenied('Task changed. Trace again before retrying.')
        conn.execute('''INSERT INTO gateway_admin_audit(inbound_id,actor_phone,setting_key,
            old_json,new_json,revision,created_at) VALUES(?,?,?,?,?,0,?)''',
            (inbound_id,phone,'retry_delivery',json.dumps({'task_id':task_id,'attempts':task['delivery_attempts']}),
             json.dumps({'status':'pending'}),utc_now()))
        audit(conn,task_id,'admin_delivery_retry',{'actor_phone':phone,'previous_attempts':task['delivery_attempts']})
    return 'Delivery retry queued. The worker will recheck eligibility before sending. No ERP decision was made.'
