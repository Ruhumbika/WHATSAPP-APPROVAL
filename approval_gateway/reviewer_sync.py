"""Retire obsolete local tasks only after an authoritative ERP resolution."""
import json
from .db import utc_now


def retire(conn, task_id, reason):
    task = conn.execute('SELECT * FROM approval_requests WHERE id=?', (task_id,)).fetchone()
    if not task or task['status'] != 'pending':
        return False
    had_draft = task['rejection_phase'] != 'closed'
    conn.execute("""UPDATE approval_requests SET status='cancelled',rejection_phase='closed',
        rejection_draft=NULL,rejection_review_token=NULL,rejection_paused=1,
        retirement_reason=?,retirement_had_draft=?,updated_at=? WHERE id=? AND status='pending'""",
        (reason, int(had_draft), utc_now(), task_id))
    conn.execute("""UPDATE decision_repeat_notifications SET status='expired',updated_at=?
        WHERE approval_request_id=? AND status='pending'""", (utc_now(), task_id))
    from .service import audit
    audit(conn, task_id, 'assignment_retired', {'reason': reason, 'unfinished_rejection_closed': had_draft})
    return True


def reconcile(conn, watch, data, notify):
    from .workflow_notifications import enqueue, digest
    current = data['progress']['current_step']
    # Explicit current-step schema required before using absence as evidence.
    if current is not None and (not isinstance(current.get('approvers'), list) or not current.get('id')):
        raise ValueError('Invalid current step')
    eligible = {str(u['id']) for u in current['approvers']} if current else set()
    tasks = conn.execute("""SELECT * FROM approval_requests WHERE source_system=? AND request_type=?
        AND source_reference_id=? AND company=? AND (status='pending' OR retirement_reason IS NOT NULL)""",
        (watch['source_system'], watch['request_type'], watch['reference_id'], data['company'])).fetchall()
    for row in tasks:
        task = dict(row)
        if not str(task['actor_user_id'] or '').isdigit():
            continue
        still_required = current and str(current['id']) == task['step_id'] and task['actor_user_id'] in eligible
        if task['status'] == 'pending' and not still_required:
            retire(conn, task['id'], 'ERP no longer requires your action at this step.')
            task = dict(conn.execute('SELECT * FROM approval_requests WHERE id=?', (task['id'],)).fetchone())
        if task['status'] != 'cancelled' or not task['retirement_reason'] or task['retirement_notice_queued'] or not notify:
            continue
        if task['delivery_status'] not in ('sent', 'simulated') and not task['retirement_had_draft']:
            continue
        # A replacement task at the same stage is an updated snapshot, not a completed action.
        replacement = conn.execute("""SELECT 1 FROM approval_requests WHERE source_system=? AND company=?
            AND request_type=? AND source_reference_id=? AND actor_directory_uuid=? AND step_id=?
            AND status='pending' AND id!=?""", (task['source_system'], task['company'], task['request_type'],
            task['source_reference_id'], task['actor_directory_uuid'], task['step_id'], task['id'])).fetchone()
        had_draft = any(r['actor_directory_uuid'] == task['actor_directory_uuid'] and (r['retirement_had_draft'] or r['rejection_phase'] != 'closed') for r in tasks)
        recipient = {'_had_draft': bool(had_draft), 'id': int(task['actor_user_id']), 'username': task['actor_name'] or 'Reviewer',
                     '_retired_task': task['id'], '_replaced': bool(replacement)}
        # One notice per reviewer/request/verified state, even if multiple old task versions existed.
        key = digest(['retired', watch['id'], task['actor_directory_uuid'], data['state_key']])
        enqueue(conn, watch, 'retired', data['state_key'], recipient, key)
        conn.execute('UPDATE approval_requests SET retirement_notice_queued=1 WHERE id=?', (task['id'],))
