# v2.1 — PDF requisition approvals

Read DOCUMENT_APPROVAL_SETUP.md for upgrade, template and testing instructions.

# WhatsApp Approval Gateway v2

Upgrade of the supplied gateway. Python 3.10+, standard library only.
Tested integration prototype; not deployed into your existing bot or real PETA backend.

## Changes

Per-source/per-step idempotency; content conflicts rejected; expiry checked at decision time;
scoped authenticated reads; cancellation; disabled-approver check; actor snapshots and audit;
template/interactive buttons and contextual approve/go on/reject text; batch webhook processing;
duplicate message protection; separate decision and execution states; persisted outgoing and
callback retries; registered per-source callback URL/secret; local CLI administration.

No original credentials, SQLite database, logs or caches included.

## Local setup

Run from the extracted project directory:
    cp config.example.env .env
    python3 -m approval_gateway.seed --approver-phone YOUR_REAL_APPROVER_PHONE

Save the generated API key. Use international phone digits. Seed is DEMO data.
Inspect/select the approver ID and map its REAL source system user. On a fresh database boss is ID 1.
    python3 -m approval_gateway.register --system peta --callback-url http://127.0.0.1:8000/api/v1/whatsapp-approvals/callback --callback-secret YOUR_RANDOM_SECRET_AT_LEAST_32_CHARACTERS --approver-id 1 --source-user-id YOUR_REAL_PETA_USER_ID
    python3 -m approval_gateway.server

Run the worker periodically through a supervisor scheduler, e.g. every 10 seconds:
    python3 -m approval_gateway.worker

DRY_RUN_WHATSAPP=true simulates outgoing sends ONLY; source callbacks still require a receiver.
Create a task:
    curl -X POST http://127.0.0.1:8088/api/approval-requests -H 'Authorization: Bearer YOUR_GENERATED_KEY' -H 'Content-Type: application/json' --data-binary @sample_approval_request.json

Then run the worker. Repeat identical requests to retrieve the existing task.
Use a new step_id for the next authorized step, or workflow_version for changed record content.
Changing an existing task's content with its same identity returns HTTP 409.

## Endpoints

- GET /health
- POST /api/approval-requests (Bearer key)
- GET /api/approval-requests/{AGR-reference} (same-source Bearer key)
- POST /api/approval-requests/{AGR-reference}/cancel (same-source Bearer key)
- GET /webhook/whatsapp (Meta verification)
- POST /webhook/whatsapp (Meta signature)

Required strings: company, request_type, reference_id, message, step_id, workflow_version.
Optional: source_system, amount, payload object, idempotency_key.
source_system must match the authenticated client. Legacy routing requires company=system. For explicit ERP-selected actors, send actor_directory_uuid and register an actor binding for that system/company first. Company is the record scope; source_user_id is an optional local user mapping, not a company ID.
Callback destination is registered locally, not supplied freely by callers.
No unauthenticated retry endpoint. Retry/expiry CLI wrappers invoke the worker.

Decision: pending/approved/rejected/expired/cancelled.
Execution: not_requested/pending/applied/rejected.
Delivery: pending/sending/sent/simulated.
Sent means provider submission accepted, not receipt or read confirmation.

## Source callback contract: IMPORTANT breaking change from v1

Payload: event_id, gateway_reference_id, reference_id, source_system, company, request_type,
step_id, workflow_version, status, responded_at, channel, actor, evidence, payload.
Actor includes user_id/name/phone; evidence includes message_id/original_reply.

Headers:
- X-Approval-Gateway-Timestamp
- X-Approval-Gateway-Signature: HMAC-SHA256(secret, timestamp + "." + RAW JSON bytes)
- Idempotency-Key: event_id

Receiver MUST:
1. Verify raw-body HMAC and timestamp freshness (maximum 5 minutes).
2. Lock record and current approval step in its own database.
3. Independently verify actor source user ID/permissions, company, step and workflow version.
4. Deduplicate event_id in the SAME transaction as normal approval execution.
5. Call existing source approval service and store channel/actor/evidence audit.
6. Return HTTP 200 JSON:
   {"event_id":"decision:AGR-...","execution_status":"applied"}
   or execution_status="rejected" for a permanent business rejection.
7. Return identical acknowledgement on retries of applied events.
8. Emit next-step task through a transactional outbox AFTER committing current approval.

HTTP 200 alone does NOT confirm execution. Update old callback implementations.
Python verification helper: approval_gateway.callbacks.verify_callback.
Gateway mappings are assertions; source MUST independently authorize them.

## Bot integration and chains

Reuse existing bot transport/credentials. Inside its SIGNATURE-VERIFIED webhook call
approval_gateway.meta.extract_responses(payload), then approval_gateway.service.decide(settings,response).
Continue ordinary bot handling for other messages. Do not configure competing webhook receivers.
Existing bot source was not provided: this package exposes integration points without editing it.

Source system owns approval chain. Each current step is one gateway task. Source emits next step
after previous decision is applied. Gateway does not invent chain or approve all remaining steps.
Routing selects one matching role/user. Multiple users of a role currently resolve to the first
registered matching user: extend trusted department-specific routing before deployment.

Text accepts exact approve/approved/go on/reject/rejected as a reply to a task, or with its AGR ID.
Unlinked text is ignored with an explanatory result; automatic clarification messages are not yet
sent. No unrestricted AI approval interpretation or WhatsApp group integration.

Context includes requested_by, department, reason, currency, amount, items and document_url.
Document URLs are text links. PDF uploading and in-chat full document viewing are NOT implemented.
Use appropriate authenticated/access-controlled document links.

## Existing database

BACK UP first. Configure DATABASE_PATH to old database. Startup performs additive migration.
Register integrations and user mappings. Old pending tasks lack actor snapshots/step metadata:
cancel/reissue them after migration. References/history remain preserved.
Do not run demo seed over production casually: seed replaces API keys.

## Tests

    python3 -m unittest discover -s tests -v

11 tests: idempotency/conflicts, access scope/URL restrictions, wrong actor, expiry, audit/duplicates,
cancellation/new steps, contextual text, batch/interactive messages, signed confirmed callback,
retryable delivery failure, migration columns. Real Meta/PETA end-to-end was not tested.

## Deployment boundaries / remaining work

Use HTTPS reverse proxy, process supervision and restricted network access. Bundled http.server
is a local prototype server. SQLite fits a small pilot; adopt production DB/job queue at scale.
Workers retry with capped backoff. Monitor pending tasks; no admin UI/dead-letter dashboard.
Delivery and callbacks are at-least-once, so source event deduplication is mandatory.
Decision and execution are separate: do not tell boss final approval succeeded until applied.
Automatic final-result WhatsApp notices, reminders/escalations and delivery receipt handling remain.
Template must have two body parameters (company, context) and two quick replies Approve/Reject.
Verify configured template name/language/API version against your real Meta account.
Source backend integration and real-bot wiring remain necessary before live use.


## Version 2.2: explicit ERP actor selection

Use `python -m approval_gateway.bind_actor --system peta --company COMPANY_ID --directory-uuid DIRECTORY_UUID --approver-id APPROVER_ID [--source-user-id LOCAL_USER_ID]` after independently verifying identity and phone ownership. This operator command records a mapping; it does not perform identity verification. Omit source-user-id when the source ERP does not need a local mapping. Never infer identity by equal numeric IDs.

Send `actor_directory_uuid` in POST /api/approval-requests alongside company, reference_id, step_id, workflow_version and payload. The ERP selects the current approver. Requests snapshot the identity; decisions reject revoked or changed bindings. Callback actor contains directory_uuid and optional user_id. ERP must revalidate current company membership, permissions, step, version and eligible items, and execute transactionally with event deduplication before acknowledging applied. Gateway decision is not proof of ERP authorization or execution.

For the submitted English template set WHATSAPP_TEMPLATE_NAME=workflow_approval_document and provide payload.document_media_id, document_filename, reference_id, workflow_step, requested_by, details, decision_scope. Six body parameters follow exactly this order: company, reference, workflow step, requested by, details, decision scope. Existing document template remains supported.

Implemented here: explicit actor bindings, separate system/company scope, actor evidence callbacks, template mapping. Existing signed callback retries and idempotent intake retained. Not implemented: the proposed complete v1 contract, public identity verification endpoints, Directory permission adapter, or PETA transactional execution callback. No live VPS or WhatsApp configuration has been modified.
