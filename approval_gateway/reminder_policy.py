"""Pause policy for proactive repeat deliveries, separate from user-requested actions."""
import json
from .db import connection, utc_now

PAUSED_DELIVERY = 'Automatic renewal paused by admin'


def paused(conn):
    row = conn.execute('SELECT values_json FROM gateway_controls WHERE id=1').fetchone()
    return bool(row and json.loads(row['values_json']).get('reminders_paused') is True)


def automatic_renewal(conn, task):
    created = conn.execute("SELECT details_json FROM audit_log WHERE request_id=? AND event='created' ORDER BY id DESC LIMIT 1",
                           (task['id'],)).fetchone()
    if created:
        origin = json.loads(created['details_json']).get('renewal_origin')
        if origin:
            return origin == 'automatic'
    # Old inbox renewals already contain explicit user-request evidence.
    explicit = conn.execute("""SELECT 1 FROM audit_log WHERE request_id=? AND event='renewed_from'
        AND json_extract(details_json,'$.trigger')='inbox'""", (task['id'],)).fetchone()
    if explicit:
        return False
    # Older releases have no origin column but do record this exact renewal key.
    return bool(task['idempotency_key'] and conn.execute("""SELECT 1 FROM audit_log
        WHERE event='renewal_verified' AND json_extract(details_json,'$.new_idempotency_key')=? LIMIT 1""",
        (task['idempotency_key'],)).fetchone())


def defer_delivery(settings, task):
    """Called after claim and again after ERP/PDF I/O; never touches decisions."""
    with connection(settings) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not paused(conn) or not automatic_renewal(conn, task):
            return False
        changed = conn.execute("""UPDATE approval_requests SET delivery_status='paused',delivery_error=?,
            delivery_attempts=MAX(0,delivery_attempts-1),updated_at=?
            WHERE id=? AND status='pending' AND delivery_status='sending'""",
            (PAUSED_DELIVERY,utc_now(),task['id']))
        if changed.rowcount:
            conn.execute("""INSERT INTO audit_log(request_id,event,details_json,created_at)
                VALUES(?,'automatic_delivery_paused','{}',?)""",(task['id'],utc_now()))
        return True


def resume_deliveries(conn, now):
    if not paused(conn):
        conn.execute("""UPDATE approval_requests SET delivery_status='pending',delivery_error=NULL,
            delivery_next_at=?,updated_at=? WHERE status='pending' AND delivery_status='paused'
            AND delivery_error=? AND expires_at>?""",(now,now,PAUSED_DELIVERY,now))
