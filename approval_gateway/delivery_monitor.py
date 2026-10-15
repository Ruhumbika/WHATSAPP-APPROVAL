"""Authenticated Meta delivery receipts; provider acceptance is not delivery."""
import re
from .db import connection, utc_now
from .admin_controls import require_admin


def init_schema(conn):
    from .message_history import init_schema as init_history
    init_history(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS delivery_receipts (
        message_id TEXT NOT NULL, status TEXT NOT NULL, event_time INTEGER NOT NULL,
        error_code TEXT, received_at TEXT NOT NULL,
        PRIMARY KEY(message_id,status,event_time))""")


def record(settings, payload):
    # Called only after server verifies the raw-body Meta signature.
    if payload.get('object') != 'whatsapp_business_account':
        return
    with connection(settings) as conn:
        for entry in payload.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})
                if change.get('field') != 'messages' or str(value.get('metadata', {}).get('phone_number_id')) != settings.whatsapp_phone_number_id:
                    continue
                for item in value.get('statuses', []):
                    if not isinstance(item, dict): continue
                    mid, status = item.get('id'), item.get('status')
                    stamp = str(item.get('timestamp', ''))
                    if not isinstance(mid, str) or not 1 <= len(mid) <= 512 or status not in ('sent','delivered','read','failed') or not re.fullmatch(r'[0-9]{1,12}', stamp):
                        continue
                    codes = ','.join(str(e.get('code')) for e in item.get('errors', []) if isinstance(e, dict) and type(e.get('code')) is int)[:120]
                    conn.execute('INSERT OR IGNORE INTO delivery_receipts VALUES(?,?,?,?,?)', (mid,status,int(stamp),codes or None,utc_now()))


def report(settings, phone):
    require_admin(settings, phone)
    with connection(settings) as conn:
        counts = conn.execute("""SELECT status,COUNT(*) n FROM (
            SELECT status,ROW_NUMBER() OVER (PARTITION BY message_id ORDER BY event_time DESC,
                CASE status WHEN 'read' THEN 4 WHEN 'delivered' THEN 3 WHEN 'failed' THEN 2 ELSE 1 END DESC) rank
            FROM delivery_receipts) WHERE rank=1 GROUP BY status""").fetchall()
        failed = conn.execute("SELECT message_id,error_code,received_at FROM delivery_receipts WHERE status='failed' ORDER BY event_time DESC LIMIT 5").fetchall()
    text = '*DELIVERY MONITOR*\nScope: gateway WhatsApp number\nMeta receipts since this update. Sent = provider accepted; delivered/read = Meta receipt.\n'
    text += '\n'.join(f"{row['status']}: {row['n']}" for row in counts) or 'No receipts received yet.'
    if failed:
        text += '\n\nRecent failure events (a later receipt may exist):'
        for row in failed:
            text += f"\n…{row['message_id'][-16:]} | Meta error {row['error_code'] or 'not supplied'} | {row['received_at']}"
    return text
