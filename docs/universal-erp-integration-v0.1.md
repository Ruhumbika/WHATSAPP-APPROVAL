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

## Credential rotation and revocation (operator-only)

Existing integration credentials can now be rotated or revoked through the
privileged CLI below. In production add `--production-approved`.
The operator must securely deliver the new key to the ERP developer.

```bash
python3 -m approval_gateway.manage_erp_credentials --system external_erp --rotate-key
python3 -m approval_gateway.manage_erp_credentials --system external_erp --revoke
```

Rotation defaults to **immediate invalidation** of the old API key.
For coordinated, non-disruptive deployment, use a bounded overlap:

```bash
python3 -m approval_gateway.manage_erp_credentials \\
  --system external_erp --rotate-key --overlap-minutes 15
```

Both keys authenticate during those 15 minutes, then the old key expires
without another restart or maintenance operation. The maximum overlap is
1,440 minutes. Additional rotation while an overlap is active is rejected.
Revocation immediately disables both keys and erases any overlap. The new
key is printed only once; deliver it securely and deploy it at the ERP.
Keep overlap short, monitor adoption and coordinate the rotation.
After the ERP confirms it has switched to the new key, close the overlap immediately:

```bash
python3 -m approval_gateway.manage_erp_credentials \\
  --system external_erp --finish-rotation
```

The new key remains active; the previous key is rejected immediately.
These commands require `--production-approved` in a production environment.

This CLI does not rotate the separate callback HMAC secret or verification
token; those require controlled operator changes. Customer-facing onboarding,
webhook updates and live end-to-end acceptance remain release gates.

## Updating the registered ERP webhook receiver

The operator can change the registered HTTPS callback destination without
altering WhatsApp/Meta settings or editing gateway Python source code:

```bash
python3 -m approval_gateway.manage_erp_webhook \\
  --system external_erp \\
  --callback-url https://erp.example.com/webhooks/new-approval
```

Use `--production-approved` only after a reviewed production change.
The operator must validate ownership of the new endpoint out-of-band first.
The command refuses to update while there are pending approval requests
**or** decisions awaiting terminal ERP acknowledgment. It does not rewrite
historical event payloads or stored callbacks. Changes are audited. There is
not yet an automatic domain ownership challenge or live webhook handshake.

## Operator configuration preflight

Once provisioning has completed, inspect an ERP without disclosing its keys:

```bash
python3 -m approval_gateway.integration_readiness --system external_erp
```

The report lists active API client, callback, company scopes and actor binding
configuration. Its `integration_ready` field intentionally remains `false`:
configuration alone cannot prove WhatsApp phone ownership, real Meta delivery,
authorized ERP business execution or webhook deduplication. External acceptance
tests are mandatory before a customer is marked production ready.

## WhatsApp is provided exclusively by our gateway

**External ERP developers do not need a Meta app, WABA, phone-number ID,
WhatsApp Cloud API token, Meta templates or Meta webhooks.** All WhatsApp
messages use the gateway's own business number, templates, verified Meta
webhook and delivery worker. ERP systems integrate only with our HTTPS API,
ERP verification endpoint and signed decision callback.

### Phone link onboarding (development branch, not production enabled)

1. The ERP signs in its own employee using its existing authentication. The
   ERP backend attests the selected Directory UUID and ERP local user ID.
2. ERP backend calls **POST /api/identity-links** with its gateway API key:

```json
{
  "company": "14",
  "actor_directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58",
  "source_user_id": "7",
  "phone_number": "255700000001"
}
```

3. The gateway responds HTTP 201 with `status`,
   `expires_at`, `whatsapp_message` and `instruction`. The
   `whatsapp_message` is a high-entropy, one-time text challenge; the ERP
   displays it ONLY to its authenticated user. The code expires after ten
   minutes.
4. That user sends the exact `LINK ...` message **from the requested phone
   to our existing WhatsApp business number**. The gateway validates the Meta
   webhook signature and receiving number before processing it. A different
   phone, reused token, disabled ERP or wrong company is rejected.
5. The ERP can check **GET
   /api/identity-links/{actor_directory_uuid}?company=14** with its gateway
   API key. No Meta API interaction is required by the ERP.

### Revocation and phone reassignment (development branch)

To revoke an existing verified identity, the ERP backend sends:

```http
POST /api/identity-links/{actor_directory_uuid}/revoke
Authorization: Bearer <GATEWAY_API_KEY>
Content-Type: application/json
```

```json
{"company":"14","source_user_id":"7"}
```

Only the same authenticated ERP, company and source-user binding may revoke.
Revocation is rejected with HTTP 409 while pending approval tasks exist.
**The ERP must cancel pending tasks and wait for outstanding decisions to receive terminal ERP acknowledgments first** through the existing
`/api/approval-requests/{id}/cancel` endpoint, or resolve them.

After revocation, `GET /api/identity-links/{uuid}?company=14` returns
`verified:false`. A future `POST /api/identity-links` may use a different
phone number, but the gateway must receive a fresh, signed inbound WhatsApp
`LINK` message from that new phone. Historic approvals and identity-link
audit events are preserved. ERP developers never need Meta app access.

A completed phone verification is tracked using `verified_at`, distinct
from `consumed_at` (which also records invalidated challenges). This avoids
mistaking a cancelled code for proof of WhatsApp phone ownership.

**Security enforcement:** Generic ERP approval creation now requires the
gateway's current successful LINK proof and latest identity-link audit event.
An operator-entered `actor_bindings` record, a claimed ERP phone number or
a historical LINK event that was subsequently revoked does not authorize a
new generic approval. The source ERP must still validate workflow permissions.

This verifies *control of the WhatsApp account that sent the message*, not
independently the real-world identity, HR employment or current ERP permission.
The ERP must authenticate the requesting employee and independently authorize
the actor, company and workflow step at creation and at decision time.
Other linked Directory identities cannot silently take over the same number.
Existing active bindings cannot be silently overwritten: the ERP must explicitly revoke and then complete a new WhatsApp phone-possession challenge.

The phone-linking API currently has no comprehensive per-ERP issuance rate
limits, staff self-service revocation or live-Meta integration acceptance; do
not enable it on production without addressing these release gates.

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

## Independent ERP sandbox reference

The standalone Python example
[`docs/integration/examples/python/mock_erp_server.py`](integration/examples/python/mock_erp_server.py)
implements the other side of the contract. It provides `POST /verify` and
`POST /callback` on loopback, stores a test record in its **own** SQLite
database, authenticates the verifier bearer token, verifies signed callbacks,
checks the actor/record/step/version again before execution, records
`event_id` in the same transaction as the business update, acknowledges
retries and rejects conflicting replays.

```bash
export ERP_VERIFY_TOKEN='replace-with-a-long-random-test-token'
export ERP_CALLBACK_SECRET='replace-with-the-provisioned-callback-secret'
python3 docs/integration/examples/python/mock_erp_server.py
```

This sample is for sandbox/developer learning, **not production deployment**.
It does not establish real Directory permissions or WhatsApp phone ownership.
Configure the gateway callback and verification URLs against HTTPS endpoints
when running an integration test; production rejects arbitrary HTTP callback
URLs.

## Required acceptance before public release

1. Independent external mock ERP sends a request and receives a real WhatsApp decision.
2. ERP verifies callback HMAC and applies business action exactly once.
3. Unverified actor, wrong company, wrong sender, stale step/digest and revoked assignment are rejected.
4. Gateway verifies idempotency, callback timeout/retry, conflicting responses and recovery.
5. Identity enrollment, key rotation, per-company access and multi-tenant isolation pass tests.
6. Production-like sandbox onboarding completed using only published docs.
7. Deployment tested against actual VPS configuration; existing PETA integration remains functional.

**Do not promise plug-and-play integration or production readiness until all gates pass.**
