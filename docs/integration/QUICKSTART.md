# ERP Developer Quickstart — WhatsApp Approval Gateway

> **Experimental developer contract on feature branch, not a production release.**
> No code access to the ERP is required by the gateway team. The ERP developer
> never needs a Meta account, WhatsApp access token, phone number ID, Meta app,
> or Meta webhook. All WhatsApp interaction runs on OUR Business number.

## Who builds what?

| Gateway operator (us) | ERP developer |
| --- | --- |
| Owns WhatsApp Business number and Meta webhooks | Owns ERP records, authenticated users and business decisions |
| Registers integration ID, company scopes, API key and webhook secret | Exposes an HTTPS workflow verification endpoint |
| Configures generic adapter and verification credentials | Implements signed callback receiver and transactional business action |
| Delivers interactive WhatsApp approvals and records audit evidence | Initiates approvals, checks status and reconciles final outcome |
| Operates WhatsApp identity-link service | Shows one-time link challenge to already authenticated ERP user |

## Operator onboarding (no ERP-specific gateway source edits)

1. Register new ERP and callback. Output includes a one-time API key and
   callback signing secret; deliver them privately to the ERP developer.
2. Set a strong verification bearer token **on the gateway server** and
   privately supply the same verifier credential to the ERP developer so
   their verification endpoint authenticates our calls.
3. Configure adapter via the operator tool:

```bash
python3 -m approval_gateway.provision_erp \
  --system sample_erp \
  --company 14 \
  --callback-url https://erp.example.com/webhooks/whatsapp-approval

# Set SAMPLE_ERP_VERIFY_TOKEN in gateway runtime environment first.
python3 -m approval_gateway.configure_generic_erp \
  --system sample_erp \
  --verify-url https://erp.example.com/api/whatsapp-approvals/verify \
  --verify-token-env SAMPLE_ERP_VERIFY_TOKEN

python3 -m approval_gateway.integration_readiness --system sample_erp
```

For production commands add `--production-approved`; use an approved change
window. **Both gateway API service and worker require controlled restart** to
load the configured generic adapter. Do not run these commands against the
production environment without change review. An operator must set secure
environment variables. The readiness report is configuration-only.

## ERP developer implementation

**1. Authenticate your employee inside your ERP.** Identify your local
`source_user_id`, company and Directory UUID. Current permissions and
assignments remain your ERP's responsibility.

**2. Verify phone possession through our WhatsApp number.**

```http
POST /api/identity-links
Authorization: Bearer <GATEWAY_API_KEY>
Content-Type: application/json
```

```json
{
  "company": "14",
  "actor_directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58",
  "source_user_id": "7",
  "phone_number": "255700000001"
}
```

Show the returned `whatsapp_message` to the authenticated employee only.
They send that text to **our existing WhatsApp Business number**. Query:

```http
GET /api/identity-links/0656c52c-970d-11f1-b365-6805cae19a58?company=14
Authorization: Bearer <GATEWAY_API_KEY>
```

The response `verified: true` confirms possession of that WhatsApp phone,
not ERP employment or approval rights.

**3. Implement `POST /api/whatsapp-approvals/verify` in your ERP.**
The gateway calls it with the separate verification bearer token before task
creation, message delivery and decision processing. Return `authorized:true`
and actor details only when the company, reference, step, actor, version and
permissions are current. Return an explicit denial on stale or revoked states.
See [full contract](../universal-erp-integration-v0.1.md).

**4. Create approvals from your ERP backend.**

```http
POST /api/approval-requests
Authorization: Bearer <GATEWAY_API_KEY>
Content-Type: application/json
```

```json
{
  "source_system": "sample_erp",
  "company": "14",
  "reference_id": "LEAVE-2026-001",
  "request_type": "leave_approval",
  "step_id": "manager",
  "workflow_version": "record-v1",
  "actor_directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58",
  "message": "Leave request requires review",
  "idempotency_key": "sample_erp-LEAVE-2026-001-manager-record-v1",
  "payload": {"employee_id": "7", "leave_days": 5}
}
```

**5. Receive the gateway-signed decision webhook.** Check the
`X-Approval-Gateway-Timestamp` and `X-Approval-Gateway-Signature`
headers against the exact raw request body, then lock the ERP row,
verify the actor and workflow again and apply the decision once.
Persist `event_id` with the business transaction. Reply HTTP 200:

```json
{
  "event_id": "decision:AGR-EXAMPLE",
  "execution_status": "applied"
}
```

Use the actual `event_id` received; the example above is **not** literal.
If execution is definitively refused, return `execution_status: "rejected"`.
Never return `applied` before the ERP transaction commits.

**6. Handle cancellation and phone changes.** Cancel pending tasks, then
`POST /api/identity-links/{uuid}/revoke` with `company` and
`source_user_id`. Start a new LINK challenge to verify any new number.

## Acceptance checklist (must pass before production integration)

- [ ] ERP onboarded without adding source-system-specific Python code
- [ ] Key/secret delivered securely and correct company scope enforced
- [ ] Phone linked through OUR signed WhatsApp webhook
- [ ] Wrong phone, revoked binding and stale workflow are denied
- [ ] Approve and Reject buttons work on our WhatsApp Business number
- [ ] Signed decision is applied exactly once to the ERP record
- [ ] Retry, duplicate and conflicting event IDs never repeat an action
- [ ] API key rotation, revocation, outage recovery and audit are tested
- [ ] End-to-end testing succeeds with the production-like deployment

**Reference material:** [OpenAPI](openapi/current-gateway.yaml),
[Postman collection](postman/Universal-ERP-Approval.experimental.postman_collection.json),
[standalone ERP sandbox](examples/python/mock_erp_server.py).

None of these documents alone establish live readiness. Completion requires
real WhatsApp-to-ERP acceptance on an approved environment.
