# WhatsApp Approval Gateway — ERP Integration Developer Guide

Integrate approvals from your ERP into **our WhatsApp Business number** using a server-to-server API. Your users approve or reject on WhatsApp; your ERP remains the source of truth and executes the final decision.

> **API status:** This guide describes the implemented `feature/universal-erp-integration-v1` branch. It is not yet a production release. Your operator will supply the actual gateway base URL and credentials. Do not use proposed `/v1/*` routes.

## Get started

**You build:** two HTTPS endpoints in your ERP and a small API client.  
**We provide:** WhatsApp number, Meta integration, message templates, approval buttons, inbound WhatsApp processing, credential provisioning and signed decision delivery.

You do **not** need a Meta account, WhatsApp token, access to our gateway source code, or to share your ERP code or database.

### 1. Receive integration credentials

Ask your gateway operator for:

| Setting | What it does |
| --- | --- |
| `GATEWAY_BASE_URL` | Base address of our deployed gateway |
| `GATEWAY_API_KEY` | Authenticates your ERP's server-to-server requests |
| `SYSTEM_ID` | Registered ERP identifier, e.g. `sample_erp` |
| `COMPANY_ID` | Company authorized for your key |
| `CALLBACK_SECRET` | Verifies decision webhooks we send to your ERP |
| `VERIFY_TOKEN` | Separate token used by our gateway when calling your verification endpoint |
| `OUR_WHATSAPP_NUMBER` | Official number where employees send their one-time link code |

Your ERP must expose **two HTTPS endpoints**: a workflow verifier and a decision callback receiver. The operator registers both URLs. Keep all keys server-side; never put them in a frontend, a mobile app, source control, or logs.

### 2. Check your connection

```bash
curl "$GATEWAY_BASE_URL/api/integration-status" \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
```

The response contains your `system`, `allowed_companies`, `configuration_checks`, `active_actor_binding_count` and `configuration_complete`. Its `integration_ready` field remains `false` until live acceptance is performed. A successful HTTP response proves authentication and configuration access, **not** that approval delivery is ready.

### 3. Implement the ERP verification endpoint

Our gateway calls your ERP **before creating an approval, before sending WhatsApp delivery, and before accepting a decision**. This is how your ERP maintains control of its current permissions and record state.

```http
POST https://erp.example.com/api/whatsapp-approvals/verify
Authorization: Bearer <VERIFY_TOKEN>
Content-Type: application/json
```

Request from the gateway:

```json
{
  "company": "14",
  "reference_id": "LEAVE-2026-001",
  "request_type": "leave_approval",
  "step_id": "manager",
  "actor_directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58",
  "workflow_version": "record-v1",
  "payload": {"employee_id": "7", "leave_days": 5}
}
```

If the actor is authorized and the record is current, respond with HTTP 200:

```json
{
  "authorized": true,
  "actor": {
    "directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58",
    "source_user_id": "7",
    "phone_number": "255700000001",
    "module_id": "leave",
    "step_id": "manager",
    "record_digest": "record-v1"
  }
}
```

The verifier must authenticate `VERIFY_TOKEN`, check company, current record status, assigned employee, approval step and permissions, and return the **current** record digest. For the generic adapter, `record_digest` must match the request's `workflow_version`. Change that version whenever relevant workflow data or assignments change.

If authorization fails, return `{"authorized":false}` with HTTP 200 or deny with HTTP 403/409/422. Never return `authorized:true` when the ERP cannot verify the state. Verification is read-only and safe to repeat.

### 4. Link an employee to our WhatsApp

Your **authenticated ERP backend** requests phone verification:

```bash
curl -X POST "$GATEWAY_BASE_URL/api/identity-links" \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "company": "14",
    "actor_directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58",
    "source_user_id": "7",
    "phone_number": "255700000001"
  }'
```

HTTP 201 returns `status: "pending_phone_verification"`, `expires_at`, `whatsapp_message` and `instruction`.

Show the returned `whatsapp_message` **only to the authenticated employee**. They send that exact `LINK ...` message **from their phone to our WhatsApp number** within 10 minutes. We verify the Meta-signed inbound message and phone ownership. No Meta integration is required in your ERP.

Check the result:

```bash
curl "$GATEWAY_BASE_URL/api/identity-links/0656c52c-970d-11f1-b365-6805cae19a58?company=14" \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
```

Continue when `verified` is `true`. This proves control of the WhatsApp account; it does **not** replace your ERP's employee authentication or authorization checks.

### 5. Create an approval

Call from your ERP backend after the relevant approval step becomes pending:

```bash
curl -X POST "$GATEWAY_BASE_URL/api/approval-requests" \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "source_system": "sample_erp",
    "company": "14",
    "request_type": "leave_approval",
    "reference_id": "LEAVE-2026-001",
    "step_id": "manager",
    "workflow_version": "record-v1",
    "actor_directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58",
    "message": "Please review this leave request",
    "idempotency_key": "sample_erp-LEAVE-2026-001-manager-record-v1",
    "payload": {"employee_id": "7", "leave_days": 5}
  }'
```

The gateway responds with HTTP **201** for a new request or **200** for an identical replay:

```json
{
  "approval_request": {
    "gateway_reference_id": "AGR-EXAMPLE"
  },
  "created": true
}
```

The object above is **abbreviated**: the actual `approval_request` includes additional persisted fields. Save its `gateway_reference_id` and use it for status or cancellation. `source_system` must match the ERP registered for your API key. Send the **same** idempotency key on retries. Do not assume a request is approved simply because creation succeeded.

**Our gateway handles WhatsApp delivery and Approve/Reject buttons.** Your ERP does not send WhatsApp messages.

### 6. Receive and execute the decision

We POST the recorded WhatsApp decision to **your registered callback URL**. Example body (fields shown are part of the implemented event):

```json
{
  "event_id": "decision:AGR-EXAMPLE",
  "gateway_reference_id": "AGR-EXAMPLE",
  "reference_id": "LEAVE-2026-001",
  "source_system": "sample_erp",
  "company": "14",
  "request_type": "leave_approval",
  "step_id": "manager",
  "workflow_version": "record-v1",
  "status": "approved",
  "channel": "whatsapp",
  "actor": {
    "directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58",
    "user_id": "7",
    "name": "Sample User",
    "phone": "255700000001"
  },
  "evidence": {
    "message_id": "wamid.example",
    "original_reply": "Approve",
    "context_id": null
  },
  "payload": {
    "employee_id": "7",
    "leave_days": 5,
    "_gateway_workflow": {
      "module_id": "leave",
      "step_id": "manager",
      "record_digest": "record-v1"
    }
  }
}
```

The actual event also includes `responded_at` and may include rejection details.

**Authenticate the callback before executing anything.** We send:

| Header | Meaning |
| --- | --- |
| `X-Approval-Gateway-Timestamp` | Unix timestamp |
| `X-Approval-Gateway-Signature` | Lowercase HMAC-SHA256 digest |
| `Idempotency-Key` | Same stable `event_id` on retries |

Compute `HMAC_SHA256(CALLBACK_SECRET, timestamp + "." + raw_request_body)`. Compare the signature in constant time. Reject callbacks older than five minutes (or over 30 seconds in the future). Verify the **original raw bytes**, not reserialized JSON.

Then, **in one ERP database transaction**:

1. Lock the actual ERP record and approval step.
2. Check company, current status, actor assignment and workflow version again.
3. Check whether `event_id` was already applied. If so, return the previously saved result without executing twice.
4. Apply your ERP's real Approve or Reject action and save the event ID and audit data in the same transaction.
5. Commit, then acknowledge HTTP 200:

```json
{
  "event_id": "decision:AGR-EXAMPLE",
  "execution_status": "applied"
}
```

Return `execution_status: "rejected"` with the **same event ID** if the ERP definitively refuses to execute the action. A plain 200 response or a message such as `{"success":true}` is **not** a valid execution acknowledgment. Our gateway retries eligible transport/temporary failures, so your receiver must be idempotent.

See the [working ERP receiver example](examples/python/mock_erp_server.py) and [HMAC verification reference](examples/python/verify_gateway_webhook.py).

### Optional: Require a rejection reason

Your ERP may opt into the gateway's existing two-choice rejection review conversation **per approval request**. Add this field inside the request's `payload`:

```json
{
  "approval_options": {
    "rejection_reason_required": true
  }
}
```

On Reject, the gateway **does not immediately execute the decision**. It asks the verified reviewer to select **Return for correction** or **Reject completely**, enter a reason and confirm it. The final signed ERP callback includes the `rejection` object with `mode` and `reason`. The ERP must interpret the mode and enforce its own business rules. Keep `approval_options` within the ERP-authorized immutable workflow snapshot.

Without this option, generic approvals use the standard immediate Approve/Reject flow. Existing PETA Requisition and Payment Advice rejection behavior remains unchanged.

## Manage approvals

All these requests require `Authorization: Bearer <GATEWAY_API_KEY>`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/integration-status` | Check ERP configuration |
| `POST` | `/api/identity-links` | Start phone verification |
| `GET` | `/api/identity-links/{uuid}?company=14` | Read verified phone status |
| `POST` | `/api/identity-links/{uuid}/revoke` | Unlink an employee |
| `POST` | `/api/approval-requests` | Create approval |
| `GET` | `/api/approval-requests/{gateway_reference_id}` | Read status and audit |
| `POST` | `/api/approval-requests/{gateway_reference_id}/cancel` | Cancel pending request |

Cancellation succeeds only while the task is pending. To change an employee's phone, cancel pending tasks, ensure previous decisions have terminal ERP acknowledgment, revoke the old link, then run a new `LINK` challenge. Historical audit records remain.

**Avoid `POST /api/workflow-events` for generic ERP onboarding.** That legacy event-trigger route requires an adapter-specific resolver and is not part of the new generic integration protocol.

## Errors and retries

The gateway currently responds with a small JSON error such as `{"error":"..."}`. Do not expect Snippe's response envelope or error codes: they belong to a different product.

| HTTP | Meaning | What to do |
| --- | --- | --- |
| `400` | Invalid fields or payload | Correct request body |
| `401` | Missing or invalid API key | Check server-side Bearer key |
| `403` | Actor, company or workflow unauthorized | Check company scope, LINK proof and ERP permissions |
| `404` | Approval/reference not found in your ERP scope | Check saved gateway reference |
| `409` | Stale/duplicate conflict or illegal state change | Refresh record, version and pending status |
| `429` | Identity challenge limit exceeded | Retry later; avoid repeated link issuance |
| `503` | ERP verifier unavailable or not configured | Check verification endpoint and operator configuration |

Do not automatically resend changed requests with a new idempotency key. Preserve the original key when retrying the same approval.

## Test integration

**Recommended order:** configuration status → verification endpoint → LINK verification → create approval → WhatsApp decision → signed callback → ERP database update → status and audit.

Run against an authorized test gateway base URL and a nonproduction ERP record. Confirm **both Approve and Reject**, wrong actor/phone, revoked access, stale workflow version, duplicate decision, callback retry and temporary ERP downtime.

For local simulated testing only, the gateway has an isolated loopback sandbox bootstrap in `approval_gateway/local_sandbox.py`. It does **not** send real WhatsApp or automatically configure an ERP verifier.

Use the [Postman collection](postman/Universal-ERP-Approval.experimental.postman_collection.json) to exercise implemented endpoints and the [OpenAPI specification](openapi/current-gateway.yaml) for machine-readable schemas.

## Operator reference (not required in ERP source code)

The gateway operator—not the external ERP developer—runs these privileged commands. All operations remain on our gateway infrastructure:

| Task | Existing operator command |
| --- | --- |
| Register ERP and callback | `python3 -m approval_gateway.provision_erp --system sample_erp --company 14 --callback-url https://erp.example.com/callback` |
| Register generic verifier | `python3 -m approval_gateway.configure_generic_erp --system sample_erp --verify-url https://erp.example.com/api/whatsapp-approvals/verify --verify-token-env SAMPLE_ERP_VERIFY_TOKEN` |
| Inspect configuration | `python3 -m approval_gateway.integration_readiness --system sample_erp` |
| Rotate API key | `python3 -m approval_gateway.manage_erp_credentials --system sample_erp --rotate-key --overlap-minutes 15` |
| Finish key rotation | `python3 -m approval_gateway.manage_erp_credentials --system sample_erp --finish-rotation` |
| Rotate callback signing secret | `python3 -m approval_gateway.manage_callback_secret --system sample_erp --stage` followed by coordinated `--activate` |
| Update callback URL | `python3 -m approval_gateway.manage_erp_webhook --system sample_erp --callback-url https://erp.example.com/new-callback` |

Production CLI changes require `--production-approved` and controlled operator review. The verifier token belongs in a secure runtime environment variable, **not** the integration JSON configuration. A gateway/worker restart is currently needed after registering a new adapter. Callback secret activation is coordinated with an ERP receiver that temporarily accepts both old and new secrets.

## Existing PETA integration

PETA is an established **legacy adapter**, not the template external developers should copy. Its requisition approval chains, payment advice flows, approval snapshots, PDF media handling and document-based WhatsApp templates remain managed independently. Its Laravel services must recheck authorization transactionally and dispatch next-step notifications after committing ERP state.

New generic ERPs use the common verifier and callback contract above and our gateway-owned `approval_request` WhatsApp template. They do **not** upload PETA requisition PDFs or need PETA's controllers, directories or database structures.

## Release status

This guide is the **single maintained ERP integration guide** in the repository. It documents implemented development-branch behavior, not a claim of production readiness. Live acceptance against an independent ERP, security review and production deployment approval remain required.

The [OpenAPI specification](openapi/current-gateway.yaml), [Postman collection](postman/Universal-ERP-Approval.experimental.postman_collection.json), and [sample receiver](examples/python/mock_erp_server.py) are executable companion artifacts, not alternative integration guides.
