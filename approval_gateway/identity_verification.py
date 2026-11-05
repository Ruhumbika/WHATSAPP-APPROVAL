"""Gateway-owned WhatsApp phone-possession proof for generic ERP actors.

This checks that an active binding is backed by the *latest* link lifecycle
event and a successfully consumed Meta-verified LINK challenge. Never infer
phone ownership from an ERP assertion or an operator-created binding alone.
"""
from __future__ import annotations

import sqlite3


def has_current_phone_proof(
    conn: sqlite3.Connection, system: str, company: str,
    directory_uuid: str, source_user_id: str, approver_id: int,
    phone_number: str,
) -> bool:
    event = conn.execute(
        """SELECT * FROM identity_link_events
           WHERE system_name=? AND company=? AND directory_uuid=?
           ORDER BY id DESC LIMIT 1""",
        (system, company, directory_uuid),
    ).fetchone()
    if event is None or event["event_type"] not in {"linked", "relinked"}:
        return False
    if (
        event["source_user_id"] != source_user_id
        or event["approver_id"] != approver_id
        or event["phone_number"] != phone_number
    ):
        return False
    confirmed = conn.execute(
        """SELECT 1 FROM identity_link_challenges
           WHERE system_name=? AND company=? AND directory_uuid=?
             AND source_user_id=? AND phone_number=?
             AND verified_at IS NOT NULL AND verified_at>=?
           LIMIT 1""",
        (system, company, directory_uuid, source_user_id,
         phone_number, event["created_at"]),
    ).fetchone()
    return confirmed is not None
