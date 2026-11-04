# WhatsApp Approval Gateway — ERP Integration Developer Guide

**Single source of truth for ERP integration • Experimental universal integration v0.1 • Development branch only**

> **Release boundary:** These endpoints describe the `feature/universal-erp-integration-v1` implementation. They are not necessarily deployed to the production gateway. Do not use the hypothetical `/v1/approvals` or `/api/v1/approval-requests` routes. Finish security and real ERP acceptance testing before production onboarding.

Our organization owns **one WhatsApp Business number, Meta app, templates, Meta webhook and delivery worker**. ERP developers **never** need Meta credentials, WhatsApp templates, direct WhatsApp API access, or access to the gateway's Python source code. Their ERP must manage its own authenticated employees, approval policy, records, audit trail, verification endpoint and signed callback receiver.

**Who uses this guide:** gateway operator onboarding a third-party ERP, external ERP backend developer (Laravel, PHP, Node, Python or any other stack), and QA tester. Example hostnames, company IDs, Directory UUIDs, API keys, and callback addresses in this document are placeholders, not active credentials.

## Integration sequence — from zero to tested

1. **Operator:** register an ERP system ID, allowed company IDs, an HTTPS callback and API credentials using operator-only commands below.
2. **Operator:** configure one generic HTTPS verification adapter with a separate bearer token; load the new adapter in the gateway and worker.
3. **ERP developer:** implement the verification endpoint; check current employee assignment, company, record state, step, workflow digest and permissions every time.
4. **ERP developer:** display the one-time identity challenge to the authenticated ERP user. The user sends `LINK ...` to **our** WhatsApp number; ERP polls our identity status API.
5. **ERP developer:** create an approval using `POST /api/approval-requests` from its backend with a stable idempotency key.
6. **Our gateway:** revalidates eligibility, delivers Approve/Reject buttons on **our WhatsApp**, and receives the signed Meta decision event.
7. **Our gateway:** sends a separately signed decision callback to the ERP's registered HTTPS endpoint.
8. **ERP developer:** validate HMAC, lock the ERP record, revalidate permissions and version, deduplicate `event_id`, apply its actual business action transactionally, return matching `event_id` + `execution_status`.
9. **ERP developer:** poll status and reconcile failures; cancel invalid pending work; rotate keys and callback secrets safely when required.
10. **QA:** test both Approve and Reject, unauthorized actor, old phone, stale digest, replay, retries and callback outages before releasing.

## Who builds what?

| Gateway operator (us) | ERP developer |
| --- | --- |
| Owns WhatsApp Business number and Meta webhooks | Owns ERP records, authenticated users and business decisions |
| Registers integration ID, company scopes, API key and webhook secret | Exposes an HTTPS workflow verification endpoint |
| Configures generic adapter and verification credentials | Implements signed callback receiver and transactional business action |
| Delivers interactive WhatsApp approvals and records audit evidence | Initiates approvals, checks status and reconciles final outcome |
| Operates WhatsApp identity-link service | Shows one-time link challenge to already authenticated ERP user |

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

## Two-phase ERP decision webhook signing-secret rotation

Our gateway uses its own WhatsApp account, but signs outgoing ERP decision
webhooks with a **separate per-integration callback HMAC secret**. To rotate
that secret without making the ERP reject our callbacks:

1. Operator stages a new secret; the current secret remains in use.
2. ERP developer configures their server to verify either current or staged
   secret using the same timestamp-and-body HMAC contract. Store both securely.
3. Operator activates the staged secret. Gateway signs subsequent callback
   attempts with the new value, including retries.
4. After observing successful callback delivery, ERP developer removes the old
   secret. The gateway never exposes Meta or WhatsApp provider credentials.

```bash
python3 -m approval_gateway.manage_callback_secret \\
  --system external_erp --stage

# ONLY after the ERP receiver accepts both secrets:
python3 -m approval_gateway.manage_callback_secret \\
  --system external_erp --activate

# Or, if rollout was cancelled prior to activation:
python3 -m approval_gateway.manage_callback_secret \\
  --system external_erp --abort
```

Use `--production-approved` for production changes. The staged secret is
printed once. The backend stores the pending secret privately until activation
or abort, and audit events do not reveal it. **There is no automatic
receiver-readiness verification**: the operator must coordinate this change
with the ERP developer and verify signed callbacks in sandbox first.

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

## Gateway API reference (implemented routes)

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

## Create an approval (implemented development-branch API)

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

### Generic versus legacy ERP trigger routes

For a new generic ERP, submit **direct approval requests** to `POST /api/approval-requests`. The older `POST /api/workflow-events` event-trigger flow requires a source adapter implementing `resolve_workflow_event`; the generic adapter does not implement this method. Do not advertise `/api/workflow-events` as a plug-and-play generic onboarding route.

## ERP developer implementation — complete integration flow

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
See the verification response and signed callback sections in this same guide.

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

**6. Handle cancellation and phone changes.** Cancel pending tasks and resolve any outstanding decision callbacks, then
`POST /api/identity-links/{uuid}/revoke` with `company` and
`source_user_id`. Start a new LINK challenge to verify any new number.

## Local isolated developer sandbox

This is a **local-only simulated environment**, not a hosted public self-service sandbox and not proof of live Meta delivery. It must use separate test database, keys and local loopback; never send real WhatsApp messages.

### Bootstrap

From a local checkout of this feature branch, in a **nonproduction** shell:

```bash
python3 -m approval_gateway.local_sandbox \
  --directory /tmp/approval-gateway-sandbox-dev \
  --port 8098
```

The command refuses `APP_ENV=production`, refuses existing folders and
creates an isolated SQLite database, private `sandbox.env` and empty
integration adapter registry. It prints a sandbox-only ERP API key and
callback secret once. Save them privately. These values are test credentials
and must never be used against production.

To run its local HTTP process, import **only** the generated sandbox
environment into a dedicated shell, then start the server:

```bash
set -a
. /tmp/approval-gateway-sandbox-dev/sandbox.env
set +a
python3 -m approval_gateway.server
```

The generated `APP_HOST=127.0.0.1` and
`DRY_RUN_WHATSAPP=true` keep the service on loopback and simulate message
delivery. The configuration explicitly clears Meta credentials and optional
WhatsApp administration features.

Check sandbox health:

```bash
curl -fsS http://127.0.0.1:8098/health
```

Run Postman against `http://127.0.0.1:8098` using the sandbox API key.
The `sandbox_erp` client is registered for company `sandbox`.

### What is not configured automatically

The bootstrap deliberately does **not** invent an independent ERP backend,
an HTTPS verification endpoint, valid actor bindings, a Meta signature or
a live WhatsApp phone conversation. Therefore creating an approval is expected
to be denied until a generic ERP verifier and signed callback receiver are
configured. The sample backend at
[`examples/python/mock_erp_server.py` ](examples/python/mock_erp_server.py)
demonstrates the ERP-side contract, but its illustrative identifiers and
record data must be aligned to sandbox company and actor details before use.

For an actual approval lifecycle test, configure a known-safe HTTPS test
verifier using `approval_gateway.configure_generic_erp`, link the actor
through a simulated signed Meta webhook with test-only Meta settings, and
run the approval worker. Keep real Meta credentials and real phone messages
out of this sandbox. Do not present a mocked callback as evidence of
production ERP execution.

### Sandbox safety / release constraints

- Never expose the loopback sandbox on a public VPS interface.
- Keep sandbox credentials, SQLite database and environment file out of Git.
- Do not run the sandbox with production environment variables or live Meta tokens.
- Sandbox API responses and dry-run status are not proof of Meta delivery.
- A separately isolated, provisioned, abuse-protected hosted sandbox is still
  required before inviting external developers.
- A real WhatsApp button-to-ERP database acceptance test is still mandatory
  before merging this feature into `main` or deploying production.

## PETA-specific legacy integration details

This section preserves operational facts for **PETA's legacy integration**, distinct from the generic protocol. New ERP developers should implement the generic verification interface rather than copying PETA's adapter or requisition-specific templates.

### PETA record, identity and workflow evidence

PETA adapter routes (relative to `/api/v1`):

- `GET requisitions/{id}/details`
- `GET requisitions/{id}/approval-chain`
- `GET whatsapp-approvals/requisitions/{id}/document` (dedicated bearer credential)

Details must expose `id`, `company_id`, `company_name`, `status`, `is_closed`, `items`, and presentation fields such as request type, cost center, dates, remarks, requester, creator and sources.

Company name is resolved in **PETA** from its company table using the requisition's `company_id`. Gateway uses that scoped snapshot for display; company ID remains the authorization scope. Do not replace company scope with the string `peta`. Do not resolve a global first-company profile for multi-company messages.

Approval chain must contain module ID, levels, boolean `is_active`, level database `id`, order `level_id`, `approval_status`, role, position and approvers. **Step ID is the database level `id`, not order/index.** Active levels are ordered by `level_id`; first outstanding pending level is selected. A rejected chain has no next step. Repeated user IDs in the same level are deduplicated.

PETA references are positive numeric strings (`"282"`), not `PETA-REQ-282` or `REQ-282`.

Lifecycle blocks DRAFT, REJECTED, CANCELLED, CLOSED or closed records. An APPROVED top-level status alone does not necessarily prove the chain is complete; pending-step resolution remains authoritative.

Directory endpoint used: `https://user-directory.abooderp.com/api/v1/users/{user_id}/permissions`. The PETA-specific identity path assumes a centrally synchronized identity and checks returned identifiers/username/email and active/ban evidence. Another ERP must define its own explicit local-ID-to-Directory mapping; never assume equal numeric IDs are sufficient. Keep mapping identity separate from company membership and permissions.

A ban marker is denied even if `is_active=1`. Missing/expired Directory authorization fails closed and prevents delivery/decision verification. A failed assigned identity can abort resolution of the event; do not silently weaken the eligibility policy to hide it.

PETA digest is SHA-256 of compact sorted JSON containing record fields except display-only `approval`, module ID, level database ID, action, and sorted assigned user IDs. Do not reproduce this hash with a different JSON/number normalization algorithm. The signed snapshot is compared with current ERP data; display progress is excluded, list order preserved.

### PETA PDF and WhatsApp templates

PETA `WhatsAppApprovalDocumentController` checks its dedicated bearer credential and calls `RequisitionController::getPrintOut($requisition, false)`. Reuse preserves the existing document layout; the integration must not set the printed flag. No PDF redesign was included in this phase.

Gateway checks application/pdf, `%PDF-` header and a 20 MiB limit, downloads from its configured ERP route, then uploads to Meta. A header check is not a full integrity scan. Media ID belongs to the sender/WABA context; it is not an arbitrary public PDF URL.

Supported templates in this baseline:

| Template | Body count including company | Notes |
|---|---:|---|
| `approval_request` | 2 | Legacy text body |
| `workflow_approval_document` | 6 | Company + reference/step/requester/details/scope |
| `requisition_approval_document` | 6 | Company + reference/requester/department/amount/reason |
| `workflow_approval_detail_document_v2` | 17 | Detailed snapshot display |

`requisition_approval_request` is NOT supported by this client. Renaming `.env` alone cannot add a new body mapping.

Detailed body parameter order:

| Number | Field |
|---:|---|
| 1 | company_name |
| 2 | reference_id |
| 3 | request_title |
| 4 | workflow_step |
| 5 | requested_by |
| 6 | created_by |
| 7 | cost_center |
| 8 | request_date |
| 9 | required_date |
| 10 | fund_direction |
| 11 | item_summary |
| 12 | subtotal_display |
| 13 | vat_display |
| 14 | total_display |
| 15 | payee_display |
| 16 | payment_method |
| 17 | reason |

Document header: numeric `document_media_id` and a plain `.pdf` filename. Body values must be non-empty strings, maximum 1,000 characters per field; detailed preparation builds them from the record snapshot. Whitespace is normalized. Button index 0: `approve_<AGR-reference>`; index 1: `reject_<AGR-reference>`.

Meta template name, language, body order, document header and button definitions must match the approved template in the sender's WABA. Approval in another WABA is not enough. Current mapping alone does not prove Meta deployment alignment. `sent` means API acceptance with a message ID, not confirmed recipient delivery/read.

### Approval state model and persistence

| Layer | States / meaning |
|---|---|
| workflow_events | pending → processing → completed; failed after exhausted/terminal handling |
| task decision | pending, approved, rejected, cancelled, expired |
| delivery | pending, sending, sent, simulated, blocked, failed, unknown |
| execution | not_requested, pending, applied, rejected |
| callback_attempts | pending, processing, sent, failed |
| notifications | pending, sending, sent, simulated, failed, unknown, expired |

Important: completed workflow event means task reconciliation/creation finished. It does not mean message delivery or approval. Cancelled tasks may retain historical pending delivery fields; worker selection also requires decision status pending, so they are not sent.

Gateway tables:

- `api_clients`: hashed integration API keys, source ownership and activity.
- `integrations`: registered callback URL and signing secret.
- `approvers`: local contact registry; descriptive role is not business authorization.
- `actor_bindings`: system + company + Directory UUID → approver and optional ERP user ID.
- `approval_requests`: immutable workflow/actor/snapshot evidence plus decision/delivery/execution state.
- `workflow_events`: durable authenticated trigger inbox and retry state.
- `callback_attempts`: durable decision outbox.
- `inbound_messages`: provider message deduplication.
- `audit_log`: task audit events.
- `decision_notifications`: unique receipt and result per task.
- `decision_repeat_notifications`: repeat-action notice unique by inbound message ID.
- Legacy tables include `approval_routing_rules`, `webhook_events`, `identities`; new integrations must not rely on legacy role routing as authorization.

PETA tables: existing requisition/approval-chain/business tables; Laravel `jobs`/failed-job infrastructure; **`approval_gateway_receipts`**. Receipt fields used: source_system, event_id, payload_hash, response_json, created_at, updated_at. Enforce a unique system/event key in the migration, not just application checks.

Gateway startup calls schema initialization/upgrade and workflow-event initialization. Notification tables are created idempotently; they are not Laravel migrations. PETA migrations are deployed using its PHP version and production migration procedure; never run migrate:fresh/reset against live data.

### PETA document template setup and PDF media upload (legacy feature)

This is an **operator-only PETA document-template workflow**, not a requirement for third-party ERPs using the generic `approval_request` template. The current WhatsApp Business Account must own and approve the selected template; a template approved on another WABA is not sufficient.

For the legacy `requisition_approval_document` template, the intended structure is:

- Language: `sw` (Swahili), when configured and approved on our sending WABA.
- Document header using a Meta-uploaded PDF media ID.
- Six positional body parameters: company, requisition reference, requester, department, formatted total, reason.
- Quick-reply buttons in order: **Approve**, **Reject**.
- PDF must be from an authorized snapshot, have a PDF signature, and comply with the gateway's 20 MiB limit.

An operator may upload a trusted local PDF using the existing helper (which performs a **real Meta upload**, not a dry-run simulation):

```bash
python3 -m approval_gateway.upload_document /absolute/path/Requisition.pdf
```

The resulting `document_media_id` can be included in the relevant PETA approval payload with an appropriate `document_filename`. See `sample_document_approval_request.json` for the legacy example. Keep `source_system`, company scope, actual record reference, step and immutable workflow version synchronized with the ERP. Do not expose Meta credentials to external ERP developers or browser code.

**Important:** Existing PETA deployments may use a different document template (including the detailed 17-parameter variant), and the live environment already manages its WhatsApp sender. Never overwrite global WhatsApp template settings merely to onboard another ERP. Use the generic text template for universal ERP integration, and test PETA-specific PDF behavior independently before changing its configuration.

### Exact ERP decision callback example

The following is an illustrative **gateway-generated** decision event, not a request the ERP developer sends to the gateway. Field names match `approval_gateway/service.py::_callback_payload`; values are examples.

```json
{
  "event_id": "decision:AGR-EXAMPLE123",
  "gateway_reference_id": "AGR-EXAMPLE123",
  "reference_id": "LEAVE-2026-001",
  "source_system": "sample_erp",
  "company": "14",
  "request_type": "leave_approval",
  "step_id": "manager",
  "workflow_version": "record-v1",
  "status": "approved",
  "responded_at": "2026-11-04T09:00:00+00:00",
  "channel": "whatsapp",
  "actor": {
    "directory_uuid": "0656c52c-970d-11f1-b365-6805cae19a58",
    "user_id": "7",
    "name": "Sample ERP User",
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

The ERP must verify these headers against the **original raw HTTP request bytes**:

```http
X-Approval-Gateway-Timestamp: <UNIX_SECONDS>
X-Approval-Gateway-Signature: <LOWERCASE_HEX_HMAC_SHA256>
Idempotency-Key: decision:AGR-EXAMPLE123
```

Compute HMAC SHA-256 with the registered ERP callback secret over `timestamp + "." + raw_body`, compare in constant time, and enforce the five-minute timestamp window (allowing at most 30 seconds of future clock skew). Check source company, assignment, current step and workflow version inside the ERP transaction; persist `event_id` in the same transaction. For a committed action return HTTP 200 with `{"event_id":"decision:AGR-EXAMPLE123","execution_status":"applied"}`. For a definitive refusal return matching `event_id` with `execution_status:"rejected"`. Plain HTTP 200 without this acknowledgment does **not** prove ERP execution.

### Historical PETA Laravel integration practices

The former local PETA patch notes contained the following **implementation patterns**, retained here as reference. They are **not instructions to overwrite currently deployed PETA controllers**. Verify existing ERP code, queue configuration and live migrations before modifying anything:

- Dispatch the source workflow notification after the requisition transaction commits, e.g. `NotifyApprovalGateway::dispatch('requisition_approval', (string) $requisition->id)->afterCommit()` when the integration is enabled.
- Ensure web and WhatsApp approval actions both use the same database row lock (typically Laravel `lockForUpdate()`) and perform the actual approval state change in a transaction. The ERP callback's receipt and business action must commit atomically.
- ERP requisition detail responses should contain the authoritative record company, lifecycle status and closed state. Do not infer authorization from frontend display data.
- A document preview must not mutate the printed flag: the existing print handler can use an optional `markPrinted` flag, while the integration-specific preview sets it to `false`. The document route must be authenticated with ERP integration credentials.
- Trigger review of the next approval level only after a successful ERP action commits. Use a durable job queue rather than a synchronous request handler; supervise both the ERP queue worker and the gateway worker.
- These PETA-specific controller and document-route patterns are **not** prerequisites for generic external ERP integrations. Generic ERPs own their own equivalent transactional enforcement and callback endpoints.

### Retired proposed API contract

An earlier, now-retired specification described **proposed** `/api/v1/*` routes, not working gateway endpoints. It has been retired to prevent accidental integration against unimplemented URLs. Use the implemented routes in this guide and the OpenAPI artifact.

## Troubleshooting and operational recovery

| Symptom | Diagnose / correction |
|---|---|
| Public workflow endpoint Apache HTML 404, local endpoint 401 | Missing Apache proxy path; local 401 proves endpoint exists and rejects missing auth |
| Unsigned callback 401 | Expected; test with valid raw-body HMAC, not a browser GET/empty unsigned POST |
| Callback 401 with signed payload | Compare secret fingerprints privately, headers, timestamp/skew, configured receiver; config clear/restart after update |
| PETA response starts `// `, HTML content type, JSONDecodeError | Text outside PHP tags in config file; PHP lint alone does not detect emitted text. Fix opening tag and validate clean response |
| `Template is not supported by this client` | Unsupported .env name; mapping and approved Meta template both must exist |
| HTTP 404 / Meta code 132001 | Check exact template/language and owning WABA against sender |
| PDF upload HTTP 401 | Token authorization/expiry; identify which credential and owning account before replacing |
| PDF upload HTTP 400 | Capture bounded Meta message/code/subcode; HTTP status alone cannot identify cause |
| Directory HTTP 401 | Expired/invalid Directory credential; independent of Meta token |
| Manual PDF succeeds but worker fails | Compare loaded configuration, process environment precedence, working directory and stale process token; restart both gateway processes after config change |
| extracted decisions=0 | May be delivery status webhook or filtered input. For actual clicks inspect sender metadata, configured Phone Number ID, payload/action format and reply context; do not call this ERP rejection |
| No new webhook logs | Check message/network, public gateway reachability and Meta subscription; avoid database action fabrication |
| approved/rejected decision with execution pending | Callback not confirmed yet; inspect callback_attempts / ERP logs |
| execution rejected but task approved | User approval recorded, ERP business validation denied; inspect failed checks and receipt |
| snapshot mismatch but CLI comparison matches | Raw JSON vs middleware null/empty-string transforms; replay may also return a cached rejection receipt |
| module not found | Run from gateway project root / correct venv and service user |
| blocked delivery remains after restart | Worker selects pending delivery only; fix cause then controlled requeue of intended task |
| unknown delivery | Acceptance ambiguous; do not blindly resend and create duplicate messages |

Snapshot incident: `sources[0].account_name` and `account_code` empty strings became null under middleware. Correct fix was raw signed JSON validation, not globally normalizing all snapshot differences away. A receipt makes a denial idempotent; diagnostics must distinguish fresh evaluation from receipt replay.

Controlled recovery must preserve decision/receipt evidence. For a never-sent task, revalidate eligibility and template, require expected task ID/system/reference/actor/status/attempt count/message ID, then atomically schedule retry. Preserve attempt history. Never bulk reset approved/rejected/cancelled tasks, fabricate inbound clicks, or remove a committed receipt to force a conflicting action. Earlier diagnostic recovery was deliberate operator work, not the routine integration path.

Gateway worker attempt limit is 8 for delivery/callback/event processing; notification limit is 5. Exponential retries and exact terminal conditions depend on exception classification. A scheduled retry can be in the future: timestamps in database are UTC; Tanzania is UTC+3. Host journal display timezone may differ; compare actual timestamps, not screen-clock assumptions.

## Release acceptance (mandatory)

1. Independent external mock ERP sends a request and receives a real WhatsApp decision.
2. ERP verifies callback HMAC and applies business action exactly once.
3. Unverified actor, wrong company, wrong sender, stale step/digest and revoked assignment are rejected.
4. Gateway verifies idempotency, callback timeout/retry, conflicting responses and recovery.
5. Identity enrollment, key rotation, per-company access and multi-tenant isolation pass tests.
6. Production-like sandbox onboarding completed using only published docs.
7. Deployment tested against actual VPS configuration; existing PETA integration remains functional.

**Do not promise plug-and-play integration or production readiness until all gates pass.**

## Technical artifacts, not competing integration guides

- **OpenAPI:** [`openapi/current-gateway.yaml`](openapi/current-gateway.yaml) — machine-readable paths and schemas.
- **Postman collection:** [`postman/Universal-ERP-Approval.experimental.postman_collection.json`](postman/Universal-ERP-Approval.experimental.postman_collection.json) — executable API requests for developers.
- **ERP sample receiver:** [`examples/python/mock_erp_server.py`](examples/python/mock_erp_server.py) — reference verification endpoint, signed callback and transactional deduplication.
- **HMAC helper:** [`examples/python/verify_gateway_webhook.py`](examples/python/verify_gateway_webhook.py) — standalone callback signature verification reference.
- **Local sandbox bootstrap:** `approval_gateway/local_sandbox.py`.
- **Provisioning and lifecycle CLI:** `approval_gateway/provision_erp.py`, `approval_gateway/configure_generic_erp.py`, `approval_gateway/manage_erp_credentials.py`, `approval_gateway/manage_callback_secret.py`, `approval_gateway/manage_erp_webhook.py`.
- **Current gateway API implementation:** `approval_gateway/server.py`, `approval_gateway/service.py`, `approval_gateway/worker.py`, `approval_gateway/integrations/generic.py`, `approval_gateway/identity_link.py`.

**Documentation policy:** This README is the **only integration guide** in the repository. Update this file rather than creating another quickstart, contract, integration tutorial or sandbox guide. OpenAPI, Postman and runnable examples have distinct executable purposes and remain separate.
