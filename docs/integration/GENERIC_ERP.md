# Universal Payload Integration — Generic ERP v1

**Goal:** Every ERP can send the SAME JSON approval request using one source-scoped Bearer API key. Gateway core remains unchanged when a new ERP is added. The operator configures an instance of `generic:build_adapter` rather than writing a new Python adapter.

## What the ERP developer receives

- `GATEWAY_BASE_URL`, `GATEWAY_API_KEY`, `SOURCE_SYSTEM`
- The API request/response and callback contract in [README.md](README.md)
- One testable approval payload and the Postman collection

The ERP also must expose **two backend endpoints**: (1) read-only verification of the current authorized approver and record, and (2) signed decision callback. This is essential: an API key authenticates the calling ERP; it cannot prove an arbitrary approver selected in a payload is authorized. These backend endpoints are registered/configured ONCE per ERP. Never use an inbound phone number as proof of approval authority.

## 1. ERP -> Gateway: create approval

`POST {GATEWAY_BASE_URL}/api/approval-requests`

```http
Authorization: Bearer YOUR_SYSTEM_KEY
Content-Type: application/json
```

```json
{
  "source_system": "ghrm",
  "company": "14",
  "request_type": "leave_approval",
  "reference_id": "LEAVE-2026-001",
  "step_id": "manager",
  "workflow_version": "record-v1",
  "actor_directory_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "message": "Employee leave application requires approval",
  "idempotency_key": "ghrm-LEAVE-2026-001-manager-record-v1",
  "payload": {
    "employee_id": 123,
    "leave_days": 5,
    "currency": "TZS",
    "document_url": "https://erp.example/leave/LEAVE-2026-001"
  }
}
```

Top-level field names are **fixed** and **not** allowed to vary across ERP systems. Business-specific values belong inside `payload`. The ERP selects the current step and proposed Directory UUID, which gateway verifies against the configured ERP verification endpoint; it never accepts a self-declared phone or permission flag. `workflow_version` must identify the current record state and must match the ERP's verified `record_digest`. When the record/step changes, use a new workflow version.

## 2. Gateway -> ERP: verify exact current workflow assignment

The generic guard sends:

```http
POST https://erp.example/api/approvals/verify
Authorization: Bearer <operator configured verification token>
Content-Type: application/json
Accept: application/json
```

```json
{
  "company": "14",
  "request_type": "leave_approval",
  "reference_id": "LEAVE-2026-001",
  "step_id": "manager",
  "actor_directory_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "workflow_version": "record-v1"
}
```

The ERP MUST look up its actual record and verify the company, current step, current assigned approver, active identity, permissions, and workflow version. It must fetch the trusted WhatsApp phone from its verified identity/Directory data, not from the initial request. Return EXACTLY:

```json
{
  "authorized": true,
  "company": "14",
  "request_type": "leave_approval",
  "reference_id": "LEAVE-2026-001",
  "step_id": "manager",
  "actor_directory_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "source_user_id": "152",
  "phone_number": "+255712345678",
  "module_id": "leave",
  "record_digest": "record-v1"
}
```

Return HTTP 403 if unauthorized. Other failures must fail closed. The gateway verifies the exact response schema and identity/scope/version. The ERP MUST revalidate again on later verification calls, including when a WhatsApp reply arrives. The gateway's callback is not proof that the ERP executed the action.

## 3. Gateway -> ERP: signed business decision callback

Configure the ERP's separate HTTPS callback URL and secret with gateway operator. Verify signature and source record permissions; perform approve/reject in a single transaction with event_id deduplication. Return HTTP 200 with `{"event_id":"decision:AGR-...","execution_status":"applied"}` **only after** commit. See [README.md](README.md) for signature and retry contract.

## 4. Operator-only: register a generic ERP integration

Create system-specific key and fixed callback using existing operator tooling. In your **operator-managed** integrations config add:

```json
{
  "systems": {
    "ghrm": {
      "enabled": true,
      "factory": "generic:build_adapter",
      "options": {
        "verification_url": "https://erp.example/api/approvals/verify",
        "token_env": "GHRM_APPROVAL_VERIFY_TOKEN"
      }
    }
  }
}
```

Merge the entry with other configured systems, do **not** replace the whole production config or remove PETA. Provision `GHRM_APPROVAL_VERIFY_TOKEN` as a separate, random, private credential of at least 32 characters. Register the corresponding source API key and callback with the operator, not using an untrusted registration payload. Generic mode has **no workflow-event resolver**, so use `POST /api/approval-requests`; `POST /api/workflow-events` is only for systems with a trusted resolver.

## 5. Acceptance checklist

1. Valid signed-in system + verified current actor + current workflow version creates ONE task.
2. Identical replay returns existing task without duplicate WhatsApp delivery.
3. Wrong API key, company, actor, step, record version or stale/disabled actor is rejected.
4. ERP verification 403, timeout, malformed JSON or invalid phone fails closed.
5. Unauthorized callback, expired/replayed callback, and duplicate event_id never execute twice.
6. ERP decision acknowledgement is confirmed separately from WhatsApp button press.
7. Local tests pass before any push to main or production deployment.

**Rollout:** This is opt-in by system configuration. Existing PETA adapter and endpoints remain unchanged. Run local regression and sandbox end-to-end verification before using it with real approvals.
