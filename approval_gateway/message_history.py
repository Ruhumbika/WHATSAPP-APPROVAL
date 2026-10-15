"""Outbound transport journal and operator CSV export. No bearer/action tokens."""
import argparse
import csv
import json
import re
from datetime import datetime
from contextlib import nullcontext
import os
from .db import connection, utc_now
from .admin_controls import require_admin


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS outbound_messages(
        id INTEGER PRIMARY KEY, phone TEXT NOT NULL, message_type TEXT NOT NULL,
        content_json TEXT NOT NULL, message_id TEXT, status TEXT NOT NULL,
        error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    columns={r['name'] for r in conn.execute('PRAGMA table_info(outbound_messages)')}
    for name in ('source_system','reference_id','company'):
        if name not in columns:
            conn.execute(f'ALTER TABLE outbound_messages ADD COLUMN {name} TEXT')
    conn.execute('CREATE INDEX IF NOT EXISTS outbound_messages_filter ON outbound_messages(phone,created_at)')


def content(payload):
    kind = payload.get('type', '')
    if kind == 'text': return {'text': payload.get('text', {}).get('body', '')}
    if kind == 'template':
        t = payload.get('template', {})
        return {'template': t.get('name'), 'language': t.get('language'),
                'parameters': [p.get('text') for c in t.get('components', [])
                               if c.get('type') == 'body' for p in c.get('parameters', [])
                               if p.get('type') == 'text']}
    if kind == 'document':
        doc = payload.get('document', {})
        return {'filename': doc.get('filename'), 'caption': doc.get('caption')}
    if kind == 'interactive':
        i = payload.get('interactive', {})
        action = i.get('action', {})
        return {'body': i.get('body', {}).get('text'),
                'buttons': [b.get('reply', {}).get('title') for b in action.get('buttons', [])],
                'rows': [r.get('title') for section in action.get('sections', []) for r in section.get('rows', [])],
                'flow': action.get('name') == 'flow'}
    return {'type': kind}


def begin(settings, payload):
    with connection(settings) as conn:
        init_schema(conn)
        now = utc_now()
        system, reference, company = context(conn, payload)
        row_id = conn.execute('''INSERT INTO outbound_messages(phone,message_type,content_json,status,created_at,updated_at)
            VALUES(?,?,?,'unknown',?,?)''', (payload['to'],payload.get('type',''),
            json.dumps(content(payload),ensure_ascii=False),now,now)).lastrowid
        conn.execute('UPDATE outbound_messages SET source_system=?,reference_id=?,company=? WHERE id=?',
                     (system,reference,company,row_id))
        return row_id


def finish(settings, row_id, status, message_id=None, error_code=None):
    with connection(settings) as conn:
        conn.execute('UPDATE outbound_messages SET status=?,message_id=?,error_code=?,updated_at=? WHERE id=?',
                     (status,message_id,str(error_code) if error_code is not None else None,utc_now(),row_id))


def context(conn, payload):
    tokens = re.findall(r'(?:AGR-[A-F0-9]+|IBX-[A-F0-9]{32}|WNF-[A-F0-9]{32})', json.dumps(payload))
    for token in tokens:
        row=conn.execute('SELECT source_system,source_reference_id,company FROM approval_requests WHERE gateway_reference_id=?',(token,)).fetchone()
        if not row:
            row=conn.execute('''SELECT a.source_system,a.source_reference_id,a.company FROM inbox_replies i
                JOIN approval_requests a ON a.id=i.task_id WHERE i.token=?''',(token,)).fetchone()
        if not row:
            row=conn.execute('''SELECT w.source_system,w.reference_id AS source_reference_id,w.company
                FROM workflow_notifications n JOIN workflow_watches w ON w.id=n.watch_id WHERE n.token=?''',(token,)).fetchone()
        if row: return row['source_system'],row['source_reference_id'],row['company']
    return None,None,None


def rows(settings, phone, *, recipient=None, since=None, until=None, status=None, system=None, reference=None, offset=0, limit=50, origin=None, message_row_id=None, _conn=None):
    require_admin(settings, phone)
    if not 0 <= offset <= 1000000 or not 1 <= limit <= 500:
        raise ValueError('Invalid report page.')
    for v in (since, until):
        if v: datetime.strptime(v, '%Y-%m-%d')
    where=[];args=[]
    for column,value in [('phone',recipient),('status',status),('system',system),('reference',reference),('origin',origin),('id',message_row_id)]:
        if value is not None:
            where.append(column+'=?');args.append(value)
    if since:where.append('substr(created_at,1,10)>=?');args.append(since)
    if until:where.append('substr(created_at,1,10)<=?');args.append(until)
    with (nullcontext(_conn) if _conn is not None else connection(settings)) as conn:
        init_schema(conn)
        # Include all historical send queues, omitting rows already in the journal.
        query='''WITH links AS (
          SELECT whatsapp_message_id message_id,source_system,source_reference_id,company
            FROM approval_requests WHERE whatsapp_message_id IS NOT NULL
          UNION SELECT i.message_id,a.source_system,a.source_reference_id,a.company
            FROM inbox_replies i JOIN approval_requests a ON a.id=i.task_id WHERE i.message_id IS NOT NULL
          UNION SELECT n.message_id,a.source_system,a.source_reference_id,a.company
            FROM decision_notifications n JOIN approval_requests a ON a.id=n.approval_request_id WHERE n.message_id IS NOT NULL
          UNION SELECT n.message_id,a.source_system,a.source_reference_id,a.company
            FROM decision_repeat_notifications n JOIN approval_requests a ON a.id=n.approval_request_id WHERE n.message_id IS NOT NULL
          UNION SELECT n.message_id,w.source_system,w.reference_id,w.company
            FROM workflow_notifications n JOIN workflow_watches w ON w.id=n.watch_id WHERE n.message_id IS NOT NULL
        ), sources AS (
          SELECT 'journal' origin,o.id,o.phone,o.message_type,o.content_json,o.message_id,o.status,
            o.error_code,o.created_at,COALESCE(o.source_system,a.source_system,'') AS system,
            COALESCE(o.reference_id,a.source_reference_id,'') AS reference,COALESCE(o.company,a.company,'') AS company
            FROM outbound_messages o LEFT JOIN links a ON a.message_id=o.message_id
          UNION ALL
          SELECT 'approval',a.id,a.actor_phone,'approval','{"note":"Historical metadata only"}',
            a.whatsapp_message_id,a.delivery_status,a.delivery_error,a.created_at,
            a.source_system,a.source_reference_id,a.company FROM approval_requests a
            WHERE a.whatsapp_message_id IS NOT NULL
          UNION ALL
          SELECT 'inbox',i.id,i.phone,'menu_or_inbox','{"note":"Historical metadata only"}',i.message_id,
            i.status,i.last_error,i.created_at,COALESCE(a.source_system,''),COALESCE(a.source_reference_id,''),
            COALESCE(a.company,'') FROM inbox_replies i LEFT JOIN approval_requests a ON a.id=i.task_id
            WHERE i.message_id IS NOT NULL
          UNION ALL
          SELECT 'decision',n.id,n.phone,'decision',json_object('text',n.body),n.message_id,n.status,
            n.last_error,n.created_at,a.source_system,a.source_reference_id,a.company
            FROM decision_notifications n JOIN approval_requests a ON a.id=n.approval_request_id
            WHERE n.message_id IS NOT NULL
          UNION ALL
          SELECT 'conversation',n.id,n.phone,'conversation',json_object('text',n.body),n.message_id,n.status,
            n.last_error,n.created_at,a.source_system,a.source_reference_id,a.company
            FROM decision_repeat_notifications n JOIN approval_requests a ON a.id=n.approval_request_id
            WHERE n.message_id IS NOT NULL
          UNION ALL
          SELECT 'workflow',n.id,n.phone,'workflow','{"note":"Historical metadata only"}',n.message_id,n.status,
            n.last_error,n.created_at,w.source_system,w.reference_id,COALESCE(w.company,'')
            FROM workflow_notifications n JOIN workflow_watches w ON w.id=n.watch_id
            WHERE n.message_id IS NOT NULL
        ), unique_sources AS (
          SELECT s.* FROM sources s WHERE origin='journal' OR NOT EXISTS(
            SELECT 1 FROM outbound_messages o WHERE o.message_id=s.message_id)
        ), receipts AS (
          SELECT *,ROW_NUMBER() OVER(PARTITION BY message_id ORDER BY event_time DESC,
            CASE status WHEN 'read' THEN 4 WHEN 'delivered' THEN 3 WHEN 'failed' THEN 2 ELSE 1 END DESC) rank
            FROM delivery_receipts
        ), history AS (
          SELECT s.origin,s.id,s.phone,s.message_type,s.content_json,s.message_id,
            COALESCE(r.status,s.status) status,COALESCE(r.error_code,s.error_code) error_code,
            s.created_at,s.system,s.reference,s.company FROM unique_sources s
            LEFT JOIN receipts r ON r.message_id=s.message_id AND r.rank=1
        ) SELECT * FROM history'''
        if where: query+=' WHERE '+' AND '.join(where)
        query+=' ORDER BY created_at DESC,id DESC,origin LIMIT ? OFFSET ?'
        return [dict(r) for r in conn.execute(query,(*args,limit,offset))]


def report(settings, phone, offset=0):
    batch=rows(settings,phone,offset=offset,limit=6)
    lines=['*MESSAGE HISTORY*','UTC timestamps. accepted = Meta acknowledgement, not device delivery.',
           'Content logging starts with this update; some historical messages have metadata only.']
    for r in batch:
        lines.append(f"{r['created_at']} | {r['phone']}\n{r['message_type']} | {r['status']} | {r['system']} {r['reference']}\n"
                     + r['content_json'][:160])
    return '\n'.join(lines)[:3700],len(batch)==6


def export_csv(settings, phone, path, **filters):
    require_admin(settings, phone)
    def safe(v):
        v='' if v is None else str(v)
        return "'" + v if v.lstrip().startswith(('=','+','-','@')) else v
    columns=['id','created_at','phone','message_type','system','company','reference','status','message_id','error_code','content_json']
    # One read snapshot keeps a growing live journal from shifting export pages.
    with connection(settings) as conn:
        init_schema(conn)
        conn.commit()
        conn.execute('BEGIN')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd,'w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=columns);w.writeheader()
            offset=0
            while True:
                batch=rows(settings,phone,offset=offset,limit=500,_conn=conn,**filters)
                for r in batch:w.writerow({c:safe(r.get(c)) for c in columns})
                if len(batch)<500:break
                offset+=500


def main():
    p=argparse.ArgumentParser(description='Admin-authorized outbound message CSV report. Dates are UTC.')
    p.add_argument('--admin-phone',required=True);p.add_argument('--output',required=True)
    for name in ['recipient','since','until','status','system','reference']:p.add_argument('--'+name)
    args=vars(p.parse_args())
    from .config import load_settings
    settings=load_settings()
    phone=args.pop('admin_phone');path=args.pop('output')
    require_admin(settings,phone)
    export_csv(settings,phone,path,**args)
    print('CSV report saved.')


if __name__=='__main__':main()
