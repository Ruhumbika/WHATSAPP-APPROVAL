# WhatsApp Approval Gateway — ERP Developer Integration Kit v1.0

**Status:** Contract documentation for the current implementation. All examples are illustrative. This document is not a guarantee that a new ERP is operational without an ERP-specific authorization/resolution adapter and onboarding.

## Integration models

**A. Workflow trigger (recommended when a workflow resolver is configured):** `POST /api/workflow-events` accepts an event UUID and record identifiers; the worker resolves the authorized current approver(s). This does NOT accept an arbitrary recipient, callback URL, or approval decision. A 202 only confirms the event was stored.

**B. Direct task:** `POST /api/approval-requests` accepts an explicit current-step actor directory UUID. Gateway calls the configured ERP workflow guard to verify actor, company and step before creating the task. This is not a bypass for verification.

## Configuration prerequisites (gateway operator)

1. Register an authenticated API client scoped to the source system. Deliver its API key securely; do not store it in frontend code.
2. Register a fixed HTTPS source-system callback URL and a unique secret (minimum 32 characters) with `python3 -m approval_gateway.register --system SYSTEM --callback-url https://erp.example/api/approvals/callback`. Supply the secret via `APPROVAL_CALLBACK_SECRET` environment variable or secure prompt (not CLI args). HTTP loopback is only allowed in local/testing.
3. Configure the system workflow guard and, for workflow triggers, event resolver. Ensure verified actor directory UUID, WhatsApp phone, current step, company and permissions can be resolved.
4. Enable gateway worker and Meta configuration. API intake success is **not** WhatsApp delivery or applied approval.
5. Ensure the ERP callback receiver verifies HMAC and re-authorizes every action; gateway never substitutes for the ERP's own permissions and transaction.

## Base URL and headers

Use your deployed HTTPS gateway base URL, provided by the operator. Do not assume a domain from this guide.

```http
Authorization: Bearer <system-specific API key>
Content-Type: application/json
```

### Workflow trigger (event-based)

```http
POST /api/workflow-events
```

```json
{
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "source_system": "ghrm",
  "request_type": "leave_approval",
  "reference_id": "LEAVE-2026-001"
}
```

Exactly four fields. `event_id` must be a hyphenated UUID. Same event ID and same payload returns 200 (replay); changed payload for same event ID gives 409. New intake returns HTTP 202 `{"accepted":true,"event_id":"..."}`. Replays return HTTP 200. Worker processes asynchronously.

### Explicit approval task

```http
POST /api/approval-requests
```

```json
{
  "source_system": "ghrm",
  "company": "14",
  "request_type": "leave_approval",
  "reference_id": "LEAVE-2026-001",
  "step_id": "manager",
  "workflow_version": "1",
  "actor_directory_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "message": "Leave request requires approval",
  "idempotency_key": "ghrm-LEAVE-2026-001-manager-v1",
  "payload": {
    "employee_id": 123,
    "days": 5,
    "document_url": "https://erp.example/leave/LEAVE-2026-001"
  }
}
```

Required: `company`, `request_type`, `reference_id`, `step_id`, `workflow_version`, `actor_directory_uuid`, `message`. `source_system` is optional but if supplied must match the authenticated client; `payload` must be an object; `amount`, `idempotency_key`, and `callback_url` are optional. If `callback_url` is sent it MUST equal the gateway-registered URL. The client cannot choose an unregistered endpoint. `payload._gateway_workflow` is reserved. Unknown top-level fields are rejected.

Success: HTTP 201 for a new task, 200 for existing identical task. Shape: `{"approval_request":{...},"created":true|false}`. Use the returned gateway reference from `approval_request` for reads and cancellation. An unchanged repeated request is idempotent; changing the same task content without advancing the workflow version produces HTTP 409.

### Read and cancel

```http
GET /api/approval-requests/{AGR-REFERENCE}
POST /api/approval-requests/{AGR-REFERENCE}/cancel
```

Both require the same source-scoped Bearer key. Read shape: `{"approval_request":{...}}`. The cancel response is the gateway's cancellation result. Cancellation does not reverse a business action already applied by the ERP.

### Health

`GET /health` returns `{"status":"ok","version":"..."}` (not a readiness or end-to-end WhatsApp delivery guarantee).

## Decision callback: ERP implements this endpoint

The callback URL is configured once by the gateway operator. The gateway worker POSTs a JSON object containing `event_id`, `gateway_reference_id`, `reference_id`, `source_system`, `company`, `request_type`, `step_id`, `workflow_version`, `status`, `responded_at`, `channel`, `actor`, `evidence`, and `payload`. Treat actor/evidence as evidence of an interaction, not a grant of ERP authority.

Headers:
- `X-Approval-Gateway-Timestamp` — Unix timestamp in seconds
- `X-Approval-Gateway-Signature` — lowercase hexadecimal HMAC-SHA256 of `timestamp + "." + exact raw request bytes`, keyed by configured callback secret
- `Idempotency-Key` — same as `event_id`

ERP callback receiver MUST:
1. Verify HMAC against **raw** request bytes, constant-time compare, reject stale timestamps older than 300 seconds or more than 30 seconds in the future.
2. Start a database transaction and lock the source record/current approval step.
3. Verify source system, company, actor's current permissions, workflow version, step, and business state independently.
4. Deduplicate `event_id` atomically with the business action.
5. Apply approve/reject through the ERP's existing business service and persist the audit evidence.
6. Return HTTP 200 JSON `{"event_id":"decision:AGR-...","execution_status":"applied"}` when committed, or `"execution_status":"rejected"` on a permanent business rejection. For retries, return the same acknowledgement for an already-applied event.
7. Only after commit, enqueue the next workflow step through the ERP's transactional outbox.

An HTTP 200 without the valid execution acknowledgement is **not** proof that the ERP applied the decision. Transport is at-least-once; receiver idempotency is compulsory. Use `approval_gateway.callbacks.verify_callback` as a Python reference implementation.

## Failure behavior

- 400 invalid body, unsupported fields or business validation failure
- 401 invalid/missing Bearer credential
- 403 wrong system, actor binding or authorization
- 404 unknown endpoint or request reference
- 409 idempotency/content conflict or stale workflow
- 411 missing Content-Length
- 413 body larger than 262144 bytes
- 415 wrong Content-Type
- 422 malformed workflow/task state
- 503 ERP workflow verification unavailable

Actual error response is `{"error":"descriptive message"}`. Do not branch on human-readable error text; use HTTP codes.

## Safety and implementation boundaries

Never call these APIs directly from a public browser/mobile app with a privileged API key. Never expose callback secrets. Never allow webhook callers to select an arbitrary phone number or callback endpoint. Do not treat gateway status `approved` as `execution_status=applied`. ERP owns the approval chain; next-step triggering must follow successful ERP commit. Use unique workflow versions for modified records. Verify integrations using a local/test environment before production. Existing README sections include historical prototype notes: current server/source code takes precedence over dated README examples.

## Quick testing

Import `postman/WhatsApp-Approval-Gateway.postman_collection.json`, configure collection variables `baseUrl`, `apiKey`, `sourceSystem`, `company`, `actorDirectoryUuid`. Never commit real keys. Run API calls against a local gateway with a configured adapter, registered system, and callback. Do not test using real approver WhatsApp accounts unless explicitly intended.

OpenAPI: `openapi.yaml` in this directory. The documented API intentionally does not expose a public actor registration endpoint; registration remains an operator-controlled activity.
