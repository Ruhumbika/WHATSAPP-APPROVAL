# Universal ERP Integration Contract — experimental v0.1

**Status: development branch only; NOT production ready.** This is a versioned starting contract for external developers. It is not a replacement for the existing API. Do not share credentials through client-side code or publish real credentials in Git.

## What the provider supplies

An operator creates an ERP integration, a gateway API credential, an HTTPS callback destination and an independent callback signing secret. The operator configures `generic:build_adapter` pointing to the ERP's HTTPS workflow-verification endpoint and supplies a **separate** verifier bearer token. Operator provisioning and verified identity enrollment are not yet self-service.

All custom ERP business logic stays on the ERP side. The gateway never needs ERP source access.

## Operator onboarding (development branch)

The CLI below generates a new API key and callback signing secret, and registers the
ERP client, explicitly allowed company scopes, plus its HTTPS callback atomically. It will **refuse to overwrite existing
credentials**. Capture secrets in an approved secure vault, not chat, Git, or shell history.

```bash
python3 -m approval_gateway.provision_erp \\
  --system external_erp \\
  --company 14 \\
  --callback-url https://erp.example.com/webhooks/whatsapp-approval
```

For `APP_ENV=production`, the operator must additionally supply
`--production-approved`. This is a privileged server-side tool, **not** a
public onboarding endpoint. It does not validate domain ownership, enroll actors,
configure the workflow adapter, or activate WhatsApp delivery by itself.

After provisioning, configure the generic adapter separately using the
`INTEGRATIONS_CONFIG_PATH` file and verification credential as shown below.
A controlled gateway/worker reload is required because adapters are loaded at
process startup. This configuration is an operator responsibility in v0.1.

## Current gateway endpoints (unchanged)

* `GET /health`
* `POST /api/approval-requests`
* `GET /api/approval-requests/{AGR-ID}`
* `POST /api/approval-requests/{AGR-ID}/cancel`
* `GET /webhook/whatsapp` and `POST /webhook/whatsapp` (Meta only)

Do **not** call the proposed `/v1/approvals` or `/api/v1/approval-requests`; they are not implemented here.

## ERP workflow verification — implemented generic adapter

Gateway sends `POST` to operator-configured `verify_url`, with HTTPS and `Authorization: Bearer <VERIFIER_TOKEN>`. The verifier token is distinct from both gateway API key and callback signing secret. ERP must validate its token, check current record state, active actor, step assignment, company, workflow version, permission and snapshot; fail closed if any check cannot complete.

Example request (on create; re-check on decision may omit workflow_version and therefore must validate current state against stored payload/snapshot):

```json
{
  "company": "14",
  "request_type": "leave_approval",
  "reference_id": "LEAVE-2026-001",
  "step_id": "manager",
  "actor_directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58",
  "workflow_version": "record-v1",
  "payload": {"employee_id": "7", "leave_days": 5}
}
```

Return HTTP 200:

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

* Return `403`, `409`, `422` or `{"authorized":false}` on denied/stale actions.
* A timeout, invalid response or server error blocks the decision; never approve by fallback.
* `record_digest` must change when the material workflow record, assignments or item scope change.
* Verified identity and gateway actor binding must exist separately; a phone asserted by an ERP is **not sufficient proof of phone ownership**. For generic ERP integrations the gateway now rejects missing actor bindings or changed phones, rather than automatically trusting the ERP assertion. Identity enrollment itself is not yet a self-service capability.
* The verification endpoint is called both on intake and at decision time. It must be safe to call repeatedly and cannot mutate the ERP record.

### Configure adapter (operator)

In the integration configuration's `systems` object, add e.g.:

```json
"ghrm": {
  "enabled": true,
  "factory": "generic:build_adapter",
  "options": {
    "verify_url": "https://erp.example.com/api/whatsapp-approvals/verify",
    "verify_token_env": "GHRM_APPROVAL_VERIFY_TOKEN"
  }
}
```

Set `GHRM_APPROVAL_VERIFY_TOKEN` securely in the gateway runtime environment (at least 32 characters). Register the client and callback via the existing CLI. Do not put secrets in the JSON configuration. Restart/reload according to the production change process only after testing.

## Create an approval (current live API schema)

```http
POST /api/approval-requests
Authorization: Bearer <GATEWAY_API_KEY>
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
  "actor_directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58",
  "message": "Leave request requires review",
  "idempotency_key": "ghrm-LEAVE-2026-001-manager-record-v1",
  "payload": {"employee_id": "7", "leave_days": 5}
}
```

The `source_system` must match authenticated API client. The callback URL cannot be supplied arbitrarily; it is registered. Every actor/company binding must be provisioned and verified first.

## Decision callback to ERP (existing implementation)

Gateway POSTs the stored decision JSON to the registered callback URL using:

* `X-Approval-Gateway-Timestamp`: Unix seconds
* `X-Approval-Gateway-Signature`: lowercase hexadecimal HMAC-SHA256 over `timestamp + "." + raw_request_body`, keyed with callback signing secret
* `Idempotency-Key`: stable `event_id`

Reject signatures older than five minutes and compare in constant time. ERP must transactionally lock the record and current step, check the actor's current permission, company, workflow version and snapshot, deduplicate `event_id`, apply its own business action, and record audit evidence.

On successful application return HTTP 200 with **exactly** the same event ID and `execution_status: "applied"`. If the ERP definitively refuses execution, return `execution_status: "rejected"`. A plain 200 without a valid acknowledgment does not confirm execution. Retried events must produce the same acknowledgment without repeating the action.

The ERP owns subsequent workflow steps and can submit the next task after committing the previous step.

## Required acceptance before public release

1. Independent external mock ERP sends a request and receives a real WhatsApp decision.
2. ERP verifies callback HMAC and applies business action exactly once.
3. Unverified actor, wrong company, wrong sender, stale step/digest and revoked assignment are rejected.
4. Gateway verifies idempotency, callback timeout/retry, conflicting responses and recovery.
5. Identity enrollment, key rotation, per-company access and multi-tenant isolation pass tests.
6. Production-like sandbox onboarding completed using only published docs.
7. Deployment tested against actual VPS configuration; existing PETA integration remains functional.

**Do not promise plug-and-play integration or production readiness until all gates pass.**
