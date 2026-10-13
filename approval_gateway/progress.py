"""Read-only workflow status replies using verified identities and existing queues."""

from datetime import datetime, timedelta, timezone
import re
import json
from urllib.parse import urlencode
from .db import connection, utc_now
from .integrations.peta import PetaClient
from .integrations.registry import get_workflow_guard
from .integrations.permissions import IntegrationUnavailable, WorkflowDenied
from .service import ServiceError

COMMAND = re.compile(r"STATUS\s+(REQ|PA)\s+([1-9][0-9]*)", re.I)


def render_progress(data):
    label = (
        "Requisition"
        if data["request_type"] == "requisition_approval"
        else "Payment Advice"
    )
    lines = [
        f"*{label} #{data['reference_id']}*",
        data["status"],
        f"\n*Progress:* {data['completed_steps']} of {data['total_steps']} steps completed; {data['remaining_steps']} remaining",
    ]
    latest = data.get("latest_action")
    if latest:
        actor = (latest.get("actor") or {}).get(
            "username"
        ) or "Actor not recorded"
        lines += [
            "\n*Last action*",
            f"{latest['action']} by {actor} — {latest['position']}",
            latest.get("recorded_at") or "Time not recorded",
        ]
    current = data.get("current_step")
    if current:
        names = (
            " or ".join(u["username"] for u in current.get("approvers", []))
            or "No assigned reviewer"
        )
        lines += [
            "\n*Awaiting action*",
            f"{current['role']['name']} — {current['position']['role_name']}",
            names,
            "Waiting since: " + (data.get("waiting_since") or "Not recorded"),
        ]
    following = data.get("following_step")
    if following:
        lines += ["\n*Up next:* " + following["position"]["role_name"]]
    lines += ["\n*Approval journey*"]
    for level in data["levels"][:10]:
        status = level["approval_status"]
        marker = (
            "✓"
            if status in ("APPROVED", "PARTIALLY_APPROVED")
            else ("→" if current and level["id"] == current["id"] else "•")
        )
        actor = ((level.get("approval") or {}).get("actor") or {}).get(
            "username"
        )
        lines.append(
            f"{marker} {level['position']['role_name']}"
            + (f" — {actor}" if actor else "")
        )
    if len(data["levels"]) > 10:
        lines.append(
            f"… {len(data['levels']) - 10} further steps. Full details are available in PETA."
        )
    lines.append("\nUpdated: " + data["updated_at"])
    return "\n".join(lines)


def prepare_progress(settings, response):
    match = COMMAND.fullmatch(response.get('text', '').strip())
    button = response.get('action') == 'progress'
    if not match and not button:
        return None
    if button:
        with connection(settings) as conn:
            row = conn.execute('SELECT * FROM approval_requests WHERE gateway_reference_id=?',
                               (response.get('reference'),)).fetchone()
        if row is None:
            raise ServiceError('Unknown progress reference.', 403)
        target = dict(row)
        request_type, reference = target['request_type'], target['source_reference_id']
    else:
        request_type = 'requisition_approval' if match[1].upper() == 'REQ' else 'payment_advice_approval'
        reference = match[2]
    with connection(settings) as conn:
        bindings = conn.execute('''SELECT b.*, a.phone_number FROM actor_bindings b
            JOIN approvers a ON a.id=b.approver_id
            WHERE b.active=1 AND a.active=1 AND a.phone_number=?''', (response['phone'],)).fetchall()
        candidates = []
        for binding in bindings:
            if button and (binding['system_name'] != target['source_system'] or binding['company'] != target['company']):
                continue
            task = conn.execute('''SELECT * FROM approval_requests WHERE source_system=? AND company=?
                AND request_type=? AND source_reference_id=? ORDER BY id DESC LIMIT 1''',
                (binding['system_name'], binding['company'], request_type, reference)).fetchone()
            if task:
                candidates.append((dict(binding), dict(task)))
    if len(candidates) != 1:
        raise ServiceError('Request identity is missing or ambiguous for this phone/company.', 403)
    binding, task = candidates[0]
    guard = get_workflow_guard(binding['system_name'])
    if hasattr(guard, '_guard'):
        guard = guard._guard({'request_type': request_type})
    try:
        user, profile = guard.directory.get_service_details(user_uuid=binding['directory_uuid'])
        # Compare verified Directory identity with its existing ERP mapping.
        source_id = str(guard.directory.source_user_id(user))
        if source_id != binding['source_user_id'] or guard._international_phone(profile['phone_number']) != response['phone']:
            raise WorkflowDenied('Progress identity mismatch')
        kind = 'requisitions' if request_type == 'requisition_approval' else 'payment-advices'
        query = urlencode({'actor_user_id': source_id, 'company_id': binding['company']})
        client = PetaClient(guard.peta.base_url, guard.peta.document_access_token,
            allow_local_http=guard.peta.base_url.startswith('http://'), timeout_seconds=guard.peta.timeout_seconds)
        payload = client._get(f'whatsapp-approvals/progress/{kind}/{reference}?{query}')
        data = payload['data']
        if payload.get('success') is not True or data['reference_id'] != reference or data['request_type'] != request_type:
            raise ValueError('Progress response identity mismatch')
        body = render_progress(data)
        if len(body) > 4096:
            raise ValueError('Progress reply exceeds message limit')
    except WorkflowDenied as exc:
        raise ServiceError('Workflow progress access denied.', 403) from exc
    except (IntegrationUnavailable, KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ServiceError('Workflow progress is unavailable.', 503) from exc
    return task, data, body


def template_parameters(data):
    """Exact positional order of the approved workflow_progress_update body."""
    label = 'Requisition' if data['request_type'] == 'requisition_approval' else 'Payment Advice'
    latest = data.get('latest_action')
    last = 'No completed action recorded'
    if latest:
        actor = (latest.get('actor') or {}).get('username') or 'Actor not recorded'
        last = f"{latest['action']} by {actor} — {latest['position']}"
    current = data.get('current_step')
    waiting = 'No action pending'
    if current:
        names = ' or '.join(u['username'] for u in current.get('approvers', [])) or 'No assigned reviewer'
        waiting = f"{current['role']['name']} — {current['position']['role_name']}: {names}"
    following = data.get('following_step')
    return [
        f"{label} #{data['reference_id']}", data['status'],
        f"{data['completed_steps']} of {data['total_steps']} steps completed; {data['remaining_steps']} remaining",
        last, waiting, data.get('waiting_since') or ('Not recorded' if current else 'Not applicable'),
        following['position']['role_name'] if following else 'No further step',
    ]


def handle_progress(settings, response):
    if response.get('action') != 'progress' and not COMMAND.fullmatch(response.get('text', '').strip()):
        return None
    # Use provider time, including for template replies, to discard delayed replays.
    try:
        received = datetime.fromtimestamp(int(response['timestamp']), timezone.utc)
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise ServiceError('Valid progress message timestamp is required.', 400) from exc
    now = utc_now()
    expires = (received + timedelta(hours=23, minutes=55)).isoformat()
    if expires <= now:
        return {'processed': False, 'reason': 'expired_status_reply'}
    if received > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ServiceError('Invalid future message timestamp.', 400)
    message_id = response.get('message_id')
    if not isinstance(message_id, str) or not message_id or len(message_id) > 512:
        raise ServiceError('Valid progress message ID is required.', 400)
    with connection(settings) as conn:
        if conn.execute('SELECT 1 FROM decision_repeat_notifications WHERE inbound_message_id=?', (message_id,)).fetchone():
            return {'processed': False, 'reason': 'duplicate_message'}
    task, data, body = prepare_progress(settings, response)
    # Store routing, not authority: the worker verifies Directory and ERP again.
    payload = json.dumps({'type': 'progress', 'response': response})
    with connection(settings) as conn:
        conn.execute('''INSERT INTO decision_repeat_notifications
            (approval_request_id,inbound_message_id,kind,phone,body,next_attempt_at,expires_at,created_at,updated_at,payload_json)
            VALUES (?,?,'received',?,?,?,?,?,?,?) ON CONFLICT(inbound_message_id) DO NOTHING''',
            (task['id'], message_id, response['phone'], body, now, expires, now, now, payload))
    return {'processed': True, 'reason': 'progress_queued'}
