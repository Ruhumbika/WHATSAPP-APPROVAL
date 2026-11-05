"""Standalone minimal ERP reference server for sandbox contract testing.

Do not deploy this sample to production. Run behind loopback and use HTTPS
reverse proxy / test tunnel. Replace fixed example record with ERP services.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ACTOR = "0656c52c-970d-11f1-b365-6805cae19a58"
COMPANY = "14"
REFERENCE = "LEAVE-2026-001"
VERSION = "record-v1"
PHONE = "255700000001"


def init_store(path: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                reference_id TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                workflow_version TEXT NOT NULL,
                step_id TEXT NOT NULL,
                assigned_actor TEXT NOT NULL,
                state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_events (
                event_id TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                event_hash TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?)",
            (REFERENCE, COMPANY, VERSION, "manager", ACTOR, "pending"),
        )


def check_verification(conn, request: dict) -> dict:
    row = conn.execute(
        "SELECT * FROM records WHERE reference_id=?",
        (request.get("reference_id"),),
    ).fetchone()
    allowed = bool(
        row and row["state"] == "pending"
        and row["company"] == request.get("company")
        and row["step_id"] == request.get("step_id")
        and row["assigned_actor"] == request.get("actor_directory_uuid")
        and row["workflow_version"] == request.get("workflow_version")
        and request.get("request_type") == "leave_approval"
    )
    if not allowed:
        return {"authorized": False}
    return {
        "authorized": True,
        "actor": {
            "directory_uuid": ACTOR,
            "source_user_id": "7",
            "phone_number": PHONE,
            "module_id": "leave",
            "step_id": row["step_id"],
            "record_digest": row["workflow_version"],
        },
    }


def verify_signature(body: bytes, timestamp: str | None,
                     signature: str | None, secret: str) -> bool:
    if (not timestamp or not timestamp.isascii() or not timestamp.isdigit()
            or not signature or len(signature) != 64 or not secret):
        return False
    delta = int(time.time()) - int(timestamp)
    if delta > 300 or delta < -30:
        return False
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def apply_decision(conn, event: dict) -> dict:
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id.startswith("decision:"):
        raise ValueError("Invalid event identity")
    event_hash = hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":"),
                   allow_nan=False).encode()
    ).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    saved = conn.execute(
        "SELECT result,event_hash FROM processed_events WHERE event_id=?", (event_id,)
    ).fetchone()
    if saved:
        if saved["event_hash"] != event_hash:
            raise ValueError("Conflicting replay for event identity")
        return {"event_id": event_id, "execution_status": saved["result"]}
    actor = event.get("actor")
    row = conn.execute(
        "SELECT * FROM records WHERE reference_id=?",
        (event.get("reference_id"),),
    ).fetchone()
    valid = bool(
        row and row["state"] == "pending"
        and row["company"] == event.get("company")
        and row["step_id"] == event.get("step_id")
        and row["workflow_version"] == event.get("workflow_version")
        and event.get("source_system") == "external"
        and isinstance(actor, dict)
        and actor.get("directory_uuid") == row["assigned_actor"]
        and actor.get("user_id") == "7"
        and event.get("status") in {"approved", "rejected"}
    )
    result = "applied" if valid else "rejected"
    if valid:
        conn.execute(
            "UPDATE records SET state=? WHERE reference_id=? AND state='pending'",
            (event["status"], event["reference_id"]),
        )
    conn.execute(
        "INSERT INTO processed_events (event_id,result,event_hash) VALUES (?,?,?)",
        (event_id, result, event_hash),
    )
    return {"event_id": event_id, "execution_status": result}


def make_handler(db_path: str, verify_token: str, callback_secret: str):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: int, value: dict) -> None:
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json(400, {"error": "invalid_length"})
                return
            if not 0 < length <= 65536:
                self.send_json(413, {"error": "invalid_length"})
                return
            body = self.rfile.read(length)
            if self.path == "/verify":
                token = self.headers.get("Authorization", "")
                if not hmac.compare_digest(token, "Bearer " + verify_token):
                    self.send_json(401, {"error": "unauthorized"})
                    return
            elif self.path == "/callback":
                if not verify_signature(
                    body, self.headers.get("X-Approval-Gateway-Timestamp"),
                    self.headers.get("X-Approval-Gateway-Signature"),
                    callback_secret,
                ):
                    self.send_json(401, {"error": "invalid_signature"})
                    return
            else:
                self.send_json(404, {"error": "not_found"})
                return
            try:
                event = json.loads(body)
                if not isinstance(event, dict):
                    raise ValueError("Expected object")
            except ValueError:
                self.send_json(400, {"error": "invalid_json"})
                return
            with sqlite3.connect(db_path, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                try:
                    if self.path == "/verify":
                        result = check_verification(conn, event)
                    else:
                        result = apply_decision(conn, event)
                except (sqlite3.Error, ValueError):
                    self.send_json(409, {"error": "invalid_event"})
                    return
            self.send_json(200, result)

    return Handler


if __name__ == "__main__":
    token = os.environ.get("ERP_VERIFY_TOKEN", "")
    secret = os.environ.get("ERP_CALLBACK_SECRET", "")
    if len(token) < 32 or len(secret) < 32:
        raise SystemExit("Configure ERP_VERIFY_TOKEN and ERP_CALLBACK_SECRET (32+ chars)")
    database = os.environ.get("ERP_SANDBOX_DB", "/tmp/approval_erp_sandbox.sqlite3")
    init_store(database)
    server = ThreadingHTTPServer(
        ("127.0.0.1", int(os.environ.get("ERP_SANDBOX_PORT", "8090"))),
        make_handler(database, token, secret),
    )
    print("ERP sandbox ready on loopback; do not expose publicly without HTTPS")
    server.serve_forever()
