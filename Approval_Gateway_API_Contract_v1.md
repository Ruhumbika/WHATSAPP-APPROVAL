# Approval Gateway API Contract v1 — implementation specification

Status: proposed implementation baseline, 4 October 2026. These v1 endpoints are not yet implemented in the existing v2.1 prototype. No live ERP or Meta settings have been changed.

## Ownership
ERP owns workflow, approver assignments, action permissions, company access, effective item snapshots and execution. Gateway owns authenticated request intake, verified WhatsApp bindings, delivery, decision evidence, callback retries and tenant-scoped status/audit. Shared workspace uses server-side company/service access; membership alone does not grant document access.

User Directory is authoritative for directory identities, service access and action permissions. Its browser cache is not authoritative. Each ERP maps directory UUID to its local actor ID explicitly. Never infer identity by equal numeric IDs, email or username alone. companiesIds can supply configured local-user mappings; they are not interchangeable with the company_id of a record. Each service defines whether a mapping is required.

## Authentication and onboarding
Provision an integration ID, service ID, permitted company scopes, gateway API credential and registered HTTPS callback endpoint. Each company scope explicitly references the ERP's company ID. Bind scope to credentials; reject cross-scope requests and reads. Never accept arbitrary callback destinations in request bodies.

Use independent server credentials rather than approver browser tokens. Callback uses a distinct signing secret. Directory access credentials and exact permission endpoint are deployment dependencies, not fabricated endpoints. Directory failure prevents execution; cache freshness policy must be configured explicitly.

## Proposed endpoints
| Method | Endpoint | Purpose |
|---|---|---|
| POST | /api/v1/approval-requests | Create one decision task for one assigned actor at one workflow step |
| GET | /api/v1/approval-requests/{request_id} | Read scoped delivery, decision and execution status |
| POST | /api/v1/approval-requests/{request_id}/cancel | Cancel before decision is accepted |
| POST | /api/v1/documents | Upload PDF bytes using multipart; return scoped document ID |
| POST | /api/v1/identity-links | Start authenticated directory-user phone linking |
| GET | /api/v1/identity-links/{link_id} | Read verification status within identity scope |

Document upload validates PDF content, size and scope. Store digest and immutable bytes. Do not fetch arbitrary ERP-provided URLs. ERP uploads its generated PDF without marking the record printed. Retention and access controls apply to documents as well as requests.

Identity linking requires ERP-authenticated user context attested by a trusted server, a short-lived single-use challenge and proof from the target WhatsApp account. Profile phone alone is insufficient. Changes revoke the old binding and cancel its pending tasks. Phone ownership establishes account control, not certainty about the person holding the phone.

## Create request example
Authorization: Bearer <integration credential>
Idempotency-Key: <stable key for this task>
Content-Type: application/json

```json
{
  "company_scope_id": "peta-company-1",
  "record": {"type": "requisition", "id": "9", "reference": "REQ-0009", "version": "revision-3"},
  "workflow": {"instance_id": "req-9-cycle-1", "step_id": "chain-level-db-17", "step_order": 2, "step_label": "Accountant"},
  "action_type": "requisition_review",
  "allowed_decisions": ["approve", "reject"],
  "decision_scope": {"mode": "all_eligible_items", "item_ids": [21, 22], "snapshot_sha256": "<sha256 of ERP canonical effective snapshot>"},
  "approver": {"directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58", "source_user_id": "<explicitly mapped PETA user ID>"},
  "context": {"company_name": "PETA Holding", "requested_by": "Workshop Manager", "department": "Workshop", "amount": "250000.00", "currency": "TZS", "reason": "Replace worn parts"},
  "document_id": "doc_<scoped immutable upload ID>",
  "expires_at": "2026-10-05T13:00:00Z",
  "policy": {"reject_requires_reason": true}
}
```

IDs above are illustrative, not verified database values. service_id and integration_id are derived from registered credentials. Source user IDs are mappings, not proof of authorization. Gateway resolves a verified directory phone binding; request does not override it. ERP retains executable payload; callback returns references, not arbitrary executable URLs or commands.

A request with identical idempotency key and canonical content returns the original reference; changed content returns 409. Delivery starts only after persistence. Amounts use decimal strings. Validate action registration, scope, document digest, expiry and actor binding before queueing. Multiple approvers receive separate actor tasks associated with the same ERP step; ERP owns any/all/quorum semantics and cancels obsolete sibling tasks.

## Create response
```json
{
  "request_id": "agr_<opaque ID>",
  "created": true,
  "delivery_status": "queued",
  "decision_status": "pending",
  "execution_status": "not_started"
}
```
201 for a new task; 200 for an identical replay. Errors: 400 malformed input, 401 invalid credential, 403 scope denial, 404 scoped resource unavailable, 409 conflicting state/idempotency, 422 missing verified identity or invalid task, 413 upload too large, 429 rate limit.

## Decision and execution
Validate Meta signature against exact body, configured receiving phone/WABA, reply sender, immutable actor binding, opaque request/action reference, pending status, expiry and message deduplication. Never authorize from button text alone. Record decision and queue callback atomically. A reject requiring a reason remains uncommitted until the assigned account supplies a request-linked reason before expiry. Reject click by itself does not execute rejection.

Delivery: queued / sent / delivered / read / failed. Do not label HTTP acceptance as delivered.
Decision: pending / approved / rejected / expired / cancelled.
Execution: not_started / pending / confirmed / denied / stale / failed.
Accepted WhatsApp decision is not ERP approval completion. Delivery retries never create a new decision task. Callback retries preserve event ID and semantic payload.

## Signed callback example
```json
{
  "schema_version": "1",
  "event_id": "evt_<stable ID>",
  "event_type": "approval.decision_received",
  "request_id": "agr_<opaque ID>",
  "integration_id": "peta",
  "service_id": 14,
  "company_scope_id": "peta-company-1",
  "record": {"type": "requisition", "id": "9", "version": "revision-3"},
  "workflow": {"instance_id": "req-9-cycle-1", "step_id": "chain-level-db-17"},
  "decision": "approve",
  "actor": {"directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58", "source_user_id": "<mapped ID>", "whatsapp_id": "255787550399", "identity_binding_id": "binding_<ID>"},
  "decision_scope": {"mode": "all_eligible_items", "item_ids": [21, 22], "snapshot_sha256": "<digest>"},
  "remarks": "Approved via WhatsApp",
  "decided_at": "2026-10-04T13:30:00Z",
  "evidence": {"inbound_message_id": "wamid.<ID>", "outbound_message_id": "wamid.<ID>", "document_sha256": "<digest>"}
}
```

Headers: X-Approval-Timestamp (Unix seconds), X-Approval-Key-Id, X-Approval-Signature = sha256=<hex HMAC-SHA256(secret, timestamp + '.' + exact raw body)>.
Receiver checks signature in constant time, timestamp within configured five-minute tolerance, allowed integration scope and event idempotency. Timestamp/signature may refresh on transport retry; event ID and semantic body remain unchanged.

Before execution, ERP rechecks current directory access/permission, explicit local actor mapping, company access, active assignment, current step, record version and eligible-item snapshot. Never trust unsigned actor fields or allow request user_id to impersonate another actor. Lock/revalidate workflow record within the execution transaction, record event result and update action atomically. Duplicate event returns stored result. Old step/version is stale, not applied to the next step. Another actor/ERP browser decision may win; loser must receive a conflict/stale result.

```json
{
  "event_id": "evt_<stable ID>",
  "request_id": "agr_<opaque ID>",
  "execution_status": "confirmed",
  "result": {"record_status": "PENDING", "step_completed": true, "workflow_completed": false, "next_step_id": "chain-level-db-18"}
}
```

Matching confirmed response alone marks execution confirmed. 2xx without execution result is not proof. Business denials/stale results are terminal. Timeout, 429 and 5xx retry with backoff; after configured exhaustion mark failed and expose controlled replay using the same event ID. If ERP committed but response was lost, replay returns its persisted result.

## PETA adapter requirements grounded in supplied code
1. Resolve requisition type/module and sorted required active levels using verified ERP rules. Distinguish database level ID, level order and UI index.
2. Determine completion by required-level membership, not count of approval rows. Reject/cancel/close block new execution. No configured chain blocks approval.
3. Resolve approvers through ApprovalChain and explicit directory-to-local identity mapping; equal numeric IDs do not prove synchronization.
4. Use current effective snapshots from previous completed level. Only previously included approved items advance. Require valid distinct item IDs belonging to this requisition; no index fallback.
5. Initial WhatsApp scope: all eligible items unchanged at this step. Partial decisions and edits use ERP until a supported interaction is implemented.
6. Finalize whole requisition only when required workflow levels complete. Preserve level-level decisions separately.
7. Reject current implementation rejects whole requisition and drops actor/remarks; new execution path must preserve actor, reason and step audit.
8. Normalize PENDING versus UI APPROVAL_PENDING explicitly. Model defines PENDING; do not rely on frontend defaulting missing status.
9. Snapshot PDF generation must not set is_printed. Existing print endpoint does set it and uses original item relationships. Current company label comes from CompanyProfile::first; use scoped company context instead.
10. Current details totals omit account amounts from their material-only total calculation; compute financial summary consistently with effective approved snapshot and currency rules.
11. Preserve original record separately from snapshots, document digest, directory actor, local actor and execution outcome.
12. Reuse a validated approval application service for browser and callback; new callback cannot fix bypasses in unguarded old endpoints by itself.

## Existing prototype gap
v2.1 already provides request/status/cancel, Meta signature checks, phone matching, decision audit and callback retries. It still resolves approver using gateway routing rules, conflates company string with source system, and does not provide verified directory linking or these v1 contracts. Existing tests do not prove the new design implemented or live integration working.

## Acceptance tests for implementation
- Correct actor approves one step; later step gets its own task.
- Reject reason/actor are recorded and whole-record scope is explicit.
- Wrong sender, unverified binding, revoked permission or company mismatch cannot execute.
- Duplicate webhook/event and two concurrent actors execute at most once.
- Stale document/record/step and cancelled/closed/rejected records do not execute.
- Lost callback response after ERP commit replays persisted result.
- PDF generation leaves print flag unchanged; document and execution snapshot agree.
- Company/service isolation applies to requests, documents, audit and shared workspace.
- Multiple approvers follow ERP policy, not an invented gateway quorum.

## Implementation order and remaining deployment inputs
Implement v1 schemas and scoped intake; verified directory binding; delivery/decision/outbox; PETA application service and signed callback; acceptance tests; shared workspace. Live setup still needs directory permission endpoint/credentials, PETA middleware authentication confirmation, level relationship ordering/active rules, production host and approved Meta template. None require repeating verified webhook setup for contract development.
