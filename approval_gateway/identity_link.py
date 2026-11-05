"""Single-use WhatsApp phone linking for generic ERP integrations.

ERP requests a short-lived challenge using its gateway API key. The account
holder types the exact challenge to OUR WhatsApp number. Our signature-verified
Meta webhook supplies the sender phone. ERP never receives Meta credentials.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from .db import connection, utc_now
from .security import normalize_phone
from .service import ServiceError, get_client, _check_company_scope
from .integrations.generic import GenericWorkflowGuard
from .integrations.registry import get_workflow_guard
from .integrations.permissions import IntegrationUnavailable

_CODE_RE = re.compile(r"LINK ([A-F0-9]{40})")
_TTL_MINUTES = 10


def _validate(data):
    if not isinstance(data, dict) or set(data) != {
        "company", "actor_directory_uuid", "source_user_id", "phone_number"
    }:
        raise ServiceError("Provide company, actor_directory_uuid, source_user_id and phone_number.")
    company = data["company"]
    source_user = data["source_user_id"]
    if any(not isinstance(value, str) or not value or len(value) > 200
           or value != value.strip() for value in (company, source_user)):
        raise ServiceError("Invalid company or source user.")
    try:
        identity = str(UUID(data["actor_directory_uuid"]))
        phone = normalize_phone(data["phone_number"])
    except (ValueError, TypeError, AttributeError):
        raise ServiceError("Invalid directory UUID or phone number.") from None
    return company, identity, source_user, phone


def start(settings, key: str, data: dict) -> dict:
    """An authenticated ERP initiates a phone-possession challenge."""
    company, identity, source_user, phone = _validate(data)
    with connection(settings) as conn:
        system = get_client(conn, key)["system_name"]
        try:
            generic = isinstance(get_workflow_guard(system), GenericWorkflowGuard)
        except IntegrationUnavailable:
            generic = False
        if not generic:
            raise ServiceError("WhatsApp linking requires a configured generic ERP.", 403)
        _check_company_scope(conn, system, company)
        conn.execute("BEGIN IMMEDIATE")
        # Revalidate credentials and scopes after obtaining a write lock.
        if get_client(conn, key)["system_name"] != system:
            raise ServiceError("ERP credentials changed.", 403)
        _check_company_scope(conn, system, company)
        existing = conn.execute(
            "SELECT * FROM actor_bindings WHERE system_name=? AND company=? AND directory_uuid=?",
            (system, company, identity),
        ).fetchone()
        if existing is not None and existing["active"] == 1:
            raise ServiceError("Active identity binding already exists; revoke it first.", 409)
        # A disabled binding can only be reactivated by a fresh proof of
        # possession from the target phone. Do not mutate existing records here.
        # An active phone belonging to another Directory identity is ambiguous.
        conflict = conn.execute(
            """SELECT 1 FROM actor_bindings b
               JOIN approvers a ON a.id=b.approver_id
               WHERE a.phone_number=? AND b.active=1 AND a.active=1
                 AND b.directory_uuid != ? LIMIT 1""",
            (phone, identity),
        ).fetchone()
        if conflict:
            raise ServiceError("WhatsApp number is linked to another identity.", 409)
        # Limit repeated challenges from one ERP to one target phone.
        # Issuing a new challenge does not reset this rolling limit.
        since = (datetime.now(timezone.utc) - timedelta(minutes=15)).replace(
            microsecond=0
        ).isoformat()
        recent = conn.execute(
            """SELECT COUNT(*) FROM identity_link_challenges
               WHERE system_name=? AND phone_number=? AND created_at>=?""",
            (system, phone, since),
        ).fetchone()[0]
        if recent >= 5:
            raise ServiceError("Too many link challenges; try again later.", 429)
        # Expire any prior unconsumed challenge in this identity scope.
        now = utc_now()
        conn.execute(
            """UPDATE identity_link_challenges SET consumed_at=?
               WHERE system_name=? AND company=? AND directory_uuid=?
                 AND consumed_at IS NULL""",
            (now, system, company, identity),
        )
        code = secrets.token_hex(20).upper()
        expires = (
            datetime.now(timezone.utc) + timedelta(minutes=_TTL_MINUTES)
        ).replace(microsecond=0).isoformat()
        conn.execute(
            """INSERT INTO identity_link_challenges (
               system_name,company,directory_uuid,source_user_id,phone_number,
               token_hash,expires_at,created_at
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (system, company, identity, source_user, phone,
             hashlib.sha256(code.encode("ascii")).hexdigest(), expires, now),
        )
    return {
        "status": "pending_phone_verification",
        "expires_at": expires,
        "whatsapp_message": "LINK " + code,
        "instruction": "Send the exact message from the stated phone to the gateway's official WhatsApp number.",
    }


def receive(settings, response: dict) -> dict | None:
    """Only call after verified Meta signature and phone-number-ID checks."""
    if response.get("action") != "conversation":
        return None
    content = response.get("text")
    if not isinstance(content, str) or not content.upper().startswith("LINK "):
        return None
    match = _CODE_RE.fullmatch(content.strip().upper())
    if match is None:
        return {"processed": False, "reason": "invalid_link_code"}
    phone = response.get("phone")
    message_id = response.get("message_id")
    if not isinstance(message_id, str) or not message_id or len(message_id) > 512:
        return {"processed": False, "reason": "invalid_message_id"}
    try:
        phone = normalize_phone(phone)
    except ValueError:
        return {"processed": False, "reason": "invalid_sender"}
    token_hash = hashlib.sha256(match.group(1).encode("ascii")).hexdigest()
    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM inbound_messages WHERE message_id=?",
                        (message_id,)).fetchone():
            return {"processed": False, "reason": "duplicate_message"}
        item = conn.execute(
            """SELECT * FROM identity_link_challenges
               WHERE token_hash=? AND phone_number=? AND consumed_at IS NULL
                 AND expires_at>?""",
            (token_hash, phone, utc_now()),
        ).fetchone()
        if item is None:
            return {"processed": False, "reason": "invalid_or_expired_link"}
        client = conn.execute(
            "SELECT active FROM api_clients WHERE system_name=?",
            (item["system_name"],),
        ).fetchone()
        if client is None or client["active"] != 1:
            return {"processed": False, "reason": "inactive_integration"}
        _check_company_scope(conn, item["system_name"], item["company"])
        old_binding = conn.execute(
            """SELECT * FROM actor_bindings
               WHERE system_name=? AND company=? AND directory_uuid=?""",
            (item["system_name"], item["company"], item["directory_uuid"]),
        ).fetchone()
        if old_binding is not None and old_binding["active"] == 1:
            return {"processed": False, "reason": "identity_already_bound"}
        if conn.execute(
            """SELECT 1 FROM actor_bindings b
               JOIN approvers a ON a.id=b.approver_id
               WHERE a.phone_number=? AND b.active=1 AND a.active=1
                 AND b.directory_uuid != ? LIMIT 1""",
            (phone, item["directory_uuid"]),
        ).fetchone():
            return {"processed": False, "reason": "conflicting_phone_identity"}
        now = utc_now()
        cursor = conn.execute(
            """INSERT INTO approvers(name,role,company,phone_number,active,created_at,updated_at)
               VALUES (?,?,?,?,1,?,?)""",
            (item["system_name"] + ":" + item["source_user_id"],
             "verified_workflow_actor", item["company"], phone, now, now),
        )
        if old_binding is None:
            conn.execute(
                """INSERT INTO actor_bindings
                   (system_name,company,directory_uuid,source_user_id,approver_id,active)
                   VALUES (?,?,?,?,?,1)""",
                (item["system_name"], item["company"], item["directory_uuid"],
                 item["source_user_id"], cursor.lastrowid),
            )
            event_type = "linked"
        else:
            conn.execute(
                """UPDATE actor_bindings SET source_user_id=?,approver_id=?,active=1
                   WHERE id=? AND active=0""",
                (item["source_user_id"], cursor.lastrowid, old_binding["id"]),
            )
            event_type = "relinked"
        conn.execute(
            """INSERT INTO identity_link_events
               (system_name,company,directory_uuid,source_user_id,
                approver_id,phone_number,event_type,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (item["system_name"], item["company"], item["directory_uuid"],
             item["source_user_id"], cursor.lastrowid, phone, event_type, now),
        )
        conn.execute(
            "UPDATE identity_link_challenges SET consumed_at=?, verified_at=? WHERE id=? AND consumed_at IS NULL",
            (now, now, item["id"]),
        )
        conn.execute(
            "INSERT INTO inbound_messages(message_id,received_at) VALUES (?,?)",
            (message_id, now),
        )
    return {"processed": True, "reason": "phone_link_verified"}


def status(settings, key: str, identity: str, company: str) -> dict:
    """ERP polls a read-only scoped link status; no challenge token returned."""
    if not isinstance(company, str) or not company or len(company) > 200:
        raise ServiceError("Invalid company.")
    try:
        identity = str(UUID(identity))
    except (TypeError, ValueError, AttributeError):
        raise ServiceError("Invalid Directory UUID.") from None
    with connection(settings) as conn:
        system = get_client(conn, key)["system_name"]
        try:
            generic = isinstance(get_workflow_guard(system), GenericWorkflowGuard)
        except IntegrationUnavailable:
            generic = False
        if not generic:
            raise ServiceError("Link status requires a configured generic ERP.", 403)
        _check_company_scope(conn, system, company)
        from .identity_verification import has_current_phone_proof
        binding = conn.execute(
            """SELECT b.approver_id,b.source_user_id,a.phone_number
               FROM actor_bindings b JOIN approvers a ON a.id=b.approver_id
               WHERE b.system_name=? AND b.company=? AND b.directory_uuid=?
                 AND b.active=1 AND a.active=1""",
            (system, company, identity),
        ).fetchone()
        verified = binding is not None and has_current_phone_proof(
            conn, system, company, identity, binding["source_user_id"],
            binding["approver_id"], binding["phone_number"],
        )
        return {"verified": verified, "company": company, "actor_directory_uuid": identity}


def revoke(settings, key: str, identity: str, data: dict) -> dict:
    """ERP revokes one existing link; pending tasks must be cancelled first.

    No binding is deleted; historical tasks and prior audit records survive.
    A future re-link requires a fresh signed WhatsApp phone-possession proof.
    """
    if not isinstance(data, dict) or set(data) != {"company", "source_user_id"}:
        raise ServiceError("Provide company and source_user_id.")
    company, source_user = data["company"], data["source_user_id"]
    if any(not isinstance(value, str) or not value or len(value) > 200
           or value != value.strip() for value in (company, source_user)):
        raise ServiceError("Invalid company or source user.")
    try:
        identity = str(UUID(identity))
    except (TypeError, ValueError, AttributeError):
        raise ServiceError("Invalid Directory UUID.") from None
    with connection(settings) as conn:
        system = get_client(conn, key)["system_name"]
        try:
            generic = isinstance(get_workflow_guard(system), GenericWorkflowGuard)
        except IntegrationUnavailable:
            generic = False
        if not generic:
            raise ServiceError("Link revocation requires a configured generic ERP.", 403)
        _check_company_scope(conn, system, company)
        conn.execute("BEGIN IMMEDIATE")
        if get_client(conn, key)["system_name"] != system:
            raise ServiceError("ERP credentials changed.", 403)
        _check_company_scope(conn, system, company)
        binding = conn.execute(
            """SELECT b.*,a.phone_number FROM actor_bindings b
               JOIN approvers a ON a.id=b.approver_id
               WHERE b.system_name=? AND b.company=? AND b.directory_uuid=?""",
            (system, company, identity),
        ).fetchone()
        if binding is None or binding["active"] != 1:
            raise ServiceError("Active verified binding not found.", 404)
        if binding["source_user_id"] != source_user:
            raise ServiceError("Source actor identity mismatch.", 403)
        # Explicit cancellation from the ERP must precede revocation.
        # This avoids orphaned approvals that users can no longer act on.
        outstanding = conn.execute(
            """SELECT 1 FROM approval_requests
               WHERE source_system=? AND company=? AND actor_directory_uuid=?
                 AND (
                    status='pending'
                    OR (
                        status IN ('approved','rejected')
                        AND COALESCE(execution_status,'') NOT IN ('applied','rejected')
                    )
                 ) LIMIT 1""",
            (system, company, identity),
        ).fetchone()
        if outstanding:
            raise ServiceError(
                "Resolve pending approvals and outstanding ERP executions before "
                "revoking this link.", 409
            )
        now = utc_now()
        conn.execute(
            "UPDATE actor_bindings SET active=0 WHERE id=? AND active=1",
            (binding["id"],),
        )
        conn.execute(
            """UPDATE identity_link_challenges SET consumed_at=?
               WHERE system_name=? AND company=? AND directory_uuid=?
                 AND consumed_at IS NULL""",
            (now, system, company, identity),
        )
        # Disable the previous recipient without destroying historical task
        # references. A new verified link creates its own approver record.
        conn.execute(
            """UPDATE approvers SET active=0,updated_at=?
               WHERE id=? AND NOT EXISTS (
                 SELECT 1 FROM actor_bindings
                 WHERE approver_id=? AND active=1
               )""",
            (now, binding["approver_id"], binding["approver_id"]),
        )
        conn.execute(
            """INSERT INTO identity_link_events
               (system_name,company,directory_uuid,source_user_id,
                approver_id,phone_number,event_type,created_at)
               VALUES (?,?,?,?,?,?,'revoked',?)""",
            (system, company, identity, source_user, binding["approver_id"],
             binding["phone_number"], now),
        )
    return {"status": "revoked", "actor_directory_uuid": identity, "company": company}
