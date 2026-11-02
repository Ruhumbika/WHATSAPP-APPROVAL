# ERP Developer Handoff — Universal WhatsApp Approval Gateway v1.2

**Distribution:** Send this document and the Postman Collection to the backend developers of ABS, CRM, GHRM, MaCargo, Ticketing and other ERPs. These are implementation contracts and **illustrative sample values**, not an assertion that these integrations are configured or deployed. PETA already has a separate adapter; coordinate any migration with the PETA owner. Last validated locally: 295 automated tests (including 3 simulated ERP backends). No real third-party ERP or live WhatsApp delivery acceptance is implied.

## 1. What every ERP developer receives

- `GATEWAY_BASE_URL`: HTTPS URL supplied by gateway operator; **not** localhost for other servers
- `SOURCE_SYSTEM`: approved system identifier (lowercase, operator-registered)
- `GATEWAY_API_KEY`: unique per ERP, shared by secure channel, backend-only
- Standard create/read contract and Postman Collection (links below)
- ERP developer must supply their own **HTTPS verification URL**, **HTTPS decision callback URL**, trusted approval/identity logic, and a backend contact to coordinate credentials.

**Never place ERP API keys or callback secrets in client-side JavaScript, mobile apps or shared Postman exports.**

## 2. Main request: ERP → Gateway

```http
POST {GATEWAY_BASE_URL}/api/approval-requests
Authorization: Bearer {GATEWAY_API_KEY}
Content-Type: application/json
Accept: application/json
```

### Universal JSON body (same top-level names for every ERP)

```json
{
  "source_system": "ghrm",
  "company": "14",
  "request_type": "leave_approval",
  "reference_id": "LEAVE-2026-001",
  "step_id": "manager",
  "workflow_version": "record-v1",
  "actor_directory_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "message": "Leave request LEAVE-2026-001 requires approval",
  "idempotency_key": "ghrm-LEAVE-2026-001-manager-record-v1",
  "payload": {
    "employee_id": 123,
    "leave_days": 5,
    "document_url": "https://erp.example/leave/LEAVE-2026-001"
  }
}
```

Required top-level: `company`, `request_type`, `reference_id`, `step_id`, `workflow_version`, `actor_directory_uuid`, `message`. `source_system` recommended and must match the API key. `idempotency_key`, `amount` (nonnegative finite number), `payload` (object), and `callback_url` (only if exactly the already registered callback) are optional. The first payload has no claim to authority: Gateway verifies the actor/step against the ERP-owned verification endpoint. Do not send `_gateway_workflow` in `payload` (reserved), do not add arbitrary top-level keys, do not send phone numbers or self-declared permissions.

Success for newly created task is HTTP 201:
```json
{
  "created": true,
  "approval_request": {
    "gateway_reference_id": "AGR-EXAMPLE",
    "source_reference_id": "LEAVE-2026-001",
    "source_system": "ghrm",
    "status": "pending",
    "execution_status": "not_requested",
    "delivery_status": "pending"
  }
}
```
The real `approval_request` has more fields. Repeating **identical** task yields HTTP 200, `created:false`, same reference. Changed content requires new workflow version and idempotency key; otherwise may yield 409. HTTP 201 **does not** mean WhatsApp was delivered or ERP approval was applied.

### Other Gateway endpoints

| Method | Path | Headers | Body | Response |
|---|---|---|---|---|
| GET | `/health` | None | None | 200 server health only |
| POST | `/api/approval-requests` | Bearer + JSON | Universal body above | 201 new / 200 same task |
| GET | `/api/approval-requests/{gateway_reference_id}` | Bearer | None | 200 `{"approval_request":{...}}` |
| POST | `/api/approval-requests/{gateway_reference_id}/cancel` | Bearer | None | Cancellation result; cannot undo ERP transaction |
| POST | `/api/workflow-events` | Bearer + JSON | Four fields `event_id,source_system,request_type,reference_id` | **NOT SUPPORTED by generic ERP mode**; only ERP-specific event resolver integrations |

## 3. Each ERP MUST implement: Gateway → ERP read-only verification

Developer implements (example path; actual URL registered with gateway operator):

```http
POST https://YOUR_ERP_DOMAIN/api/approvals/verify
Authorization: Bearer {ERP_VERIFICATION_TOKEN}
Content-Type: application/json
Accept: application/json
```

Gateway sends:
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

Gateway may omit `workflow_version` when rechecking before decision; **ERP must still independently verify the current active step, approver and record state**. Gateway expects **exactly** these ten response keys, no additional fields:
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

Check the real source record, company, approval chain, assignee, permissions, active Directory identity, verified phone and digest. Return HTTP 403 for denied or stale assignments. `record_digest` must equal incoming `workflow_version` when supplied. Do not return made-up users/phones.

## 4. Each ERP MUST implement: Gateway → ERP signed decision callback

Developer implements example:
```http
POST https://YOUR_ERP_DOMAIN/api/approvals/callback
Content-Type: application/json
X-Approval-Gateway-Timestamp: <Unix seconds>
X-Approval-Gateway-Signature: <lowercase hex HMAC-SHA256>
Idempotency-Key: <same event_id>
```

Body shape (example):
```json
{
  "event_id": "decision:AGR-EXAMPLE",
  "gateway_reference_id": "AGR-EXAMPLE",
  "reference_id": "LEAVE-2026-001",
  "source_system": "ghrm",
  "company": "14",
  "request_type": "leave_approval",
  "step_id": "manager",
  "workflow_version": "record-v1",
  "status": "approved",
  "responded_at": "2026-10-30T16:09:40+00:00",
  "channel": "whatsapp",
  "actor": {
    "directory_uuid": "550e8400-e29b-41d4-a716-446655440000",
    "user_id": "152",
    "name": "Example Approver",
    "phone": "255712345678"
  },
  "evidence": {
    "message_id": "example-whatsapp-message",
    "original_reply": "Approve",
    "context_id": null
  },
  "payload": {
    "employee_id": 123,
    "leave_days": 5
  }
}
```

Possible `status`: `approved` or `rejected`; rejection may also have a `rejection` object. Never interpret actor/evidence as permission: independently recheck record and authorization.

Signature = lowercase HMAC-SHA256 over UTF-8 bytes `timestamp + "." + exact_raw_request_body` keyed with the system's callback secret. Verify constant-time; enforce timestamp <=300 seconds old and <=30 seconds future skew. Do not parse/re-serialize before verifying HMAC. Atomically deduplicate `event_id`, apply ERP business transaction, persist audit, and respond **only after commit**:
```json
{"event_id":"decision:AGR-EXAMPLE","execution_status":"applied"}
```
For a permanently rejected business execution reply `execution_status:"rejected"` (not HTTP denial of the approver). On retries of already-committed event, return same reply. Do not send next workflow step until transaction commits. Transport is at-least-once.

## 5. Per-system illustrative create bodies

**Every example below has exactly the SAME outer JSON field names.** `source_system`, `request_type`, IDs, actor UUIDs, version and domain URLs shown here are **placeholders for discussion**, not automatically valid registrations. The developer must replace them with real system values.

### GHRM — leave
```json
{"source_system":"ghrm","company":"14","request_type":"leave_approval","reference_id":"LEAVE-2026-001","step_id":"manager","workflow_version":"record-v1","actor_directory_uuid":"550e8400-e29b-41d4-a716-446655440000","message":"Employee leave requires manager approval","idempotency_key":"ghrm-LEAVE-2026-001-manager-record-v1","payload":{"employee_id":123,"leave_days":5,"document_url":"https://erp.example/leave/LEAVE-2026-001"}}
```

### ABS — payment
```json
{"source_system":"abs","company":"14","request_type":"payment_approval","reference_id":"PAY-2026-001","step_id":"finance_manager","workflow_version":"record-v1","actor_directory_uuid":"550e8400-e29b-41d4-a716-446655440000","message":"Payment PAY-2026-001 requires approval","idempotency_key":"abs-PAY-2026-001-finance_manager-record-v1","amount":150000,"payload":{"invoice_id":456,"currency":"TZS","payment_reference":"PAY-2026-001"}}
```

### CRM — discount approval
```json
{"source_system":"crm","company":"14","request_type":"discount_approval","reference_id":"QUOTE-2026-001","step_id":"sales_manager","workflow_version":"record-v1","actor_directory_uuid":"550e8400-e29b-41d4-a716-446655440000","message":"Sales quotation discount requires approval","idempotency_key":"crm-QUOTE-2026-001-sales_manager-record-v1","payload":{"quotation_id":789,"customer_id":45,"discount_percent":10}}
```

### MaCargo — shipment approval
```json
{"source_system":"macargo","company":"14","request_type":"shipment_approval","reference_id":"SHIP-2026-001","step_id":"operations_manager","workflow_version":"record-v1","actor_directory_uuid":"550e8400-e29b-41d4-a716-446655440000","message":"Shipment SHIP-2026-001 requires approval","idempotency_key":"macargo-SHIP-2026-001-operations_manager-record-v1","payload":{"shipment_id":321,"origin":"Dar es Salaam","destination":"Morogoro"}}
```

### Ticketing — refund approval
```json
{"source_system":"ticketing","company":"14","request_type":"refund_approval","reference_id":"REFUND-2026-001","step_id":"supervisor","workflow_version":"record-v1","actor_directory_uuid":"550e8400-e29b-41d4-a716-446655440000","message":"Ticket refund requires approval","idempotency_key":"ticketing-REFUND-2026-001-supervisor-record-v1","amount":45000,"payload":{"booking_id":1122,"ticket_number":"TKT-001","currency":"TZS"}}
```

### PETA — existing specialized integration
PETA already has a source-specific adapter and workflow event resolver. Do **not** replace it with a generic adapter without PETA owner approval and regression tests. PETA requisition/payment-advice use the existing workflow contracts; a generic task body like the examples above should not be assumed equivalent to its current specialized resolver.

## 6. Backend team acceptance requirements

1. Provide actual `source_system`, company/tenancy IDs, workflow module and request types.
2. Confirm `reference_id`, `step_id`, `workflow_version`, `actor_directory_uuid` are sourced from actual ERP tables/services.
3. Provide reachable staging HTTPS verification URL and callback URL; approve separate verification and callback secrets via secure channel.
4. Successfully create a request (201), read status (200), repeat same request (200, `created:false`).
5. Verify incorrect API key/source system, revoked approver, stale workflow/record, tampered/stale signature and duplicate callback all fail safely.
6. Confirm actual ERP database update and idempotent callback ack (not merely `status:approved` inside Gateway).
7. Confirm WhatsApp send/delivery and Approve/Reject in a controlled real test with signed Meta webhook.
8. Obtain ERP owner sign-off and follow local tests → Git push/review → staging → approved production deployment and rollback.

## 7. Files to send

- [Universal Postman Collection](postman/WhatsApp-Approval-Gateway.postman_collection.json)
- [Postman Local Sandbox](postman/environments/Local-Sandbox.postman_environment.json)
- [Postman Production Template](postman/environments/Production-Template.postman_environment.json)
- [Generic ERP contract](GENERIC_ERP.md)
- [Operator onboarding checklist](ONBOARDING_CHECKLIST.md)

**Do not include live API keys, verification tokens, signing secrets or real phone numbers in this handoff.** 
