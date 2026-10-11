# WhatsApp Approval Gateway — Requisition Developer Guide

Version: 1.0 • Baseline: 9 October 2026 • Owner: Rjay / ERP DECISION

This guide documents the implemented gateway contract and the PETA Requisition integration. It is written for developers integrating another ERP or adding a new workflow. It supersedes the *proposed* contract in `Approval_Gateway_API_Contract_v1.md` for the implemented flow. Do not implement that proposal's `/api/v1/...` endpoints or signature headers as if they already exist.

## 1. Completion and evidence

| Area | Current evidence / limit |
|---|---|
| Requisition approval | Local and live approval tested; gateway execution `applied`, ERP approved |
| Requisition rejection | Local and live rejection tested; callback confirmed `applied` |
| Automatic ERP triggers | Laravel queue jobs completed; gateway events/tasks created automatically |
| Automatic worker | systemd server and worker active; cPanel cron processes PETA's dedicated queue |
| Reconciliation | Obsolete sibling pending tasks cancelled after ERP workflow change |
| PDF | Existing PETA renderer reused through a dedicated authenticated download route; upload tested |
| Confirmations | Receipt/result messages implemented; company name and English wording added |
| Repeat action | Separate notification queue implemented/tested; opposite action cannot create a second business decision |
| Latest requisition 282 | Task 32 sent on attempt 7; user subsequently reported action flow working after network issue |
| Detailed template | Local mapping tested: 17 body parameters; approved in a different WABA from the test sender; real-sender end-to-end detailed-template test remains unconfirmed |
| Active sender at last diagnostic | Test Phone Number ID `1327391220463367`, template `workflow_approval_document` |
| Intended production sender | `1326471317224727`; verified per user, not confirmed as the current gateway sender |
| Durable credentials | Automatic User Directory token renewal remains pending; stable Meta system-user token setup was blocked by account verification |
| Payment Advice | Not integrated yet; must inspect its actual ERP workflow before implementing |

Repository source was inspected for this guide; PETA-specific implementation details also come from supplied code and observed tests. The gateway checkout contains later notification patches. Record the deployed commit of BOTH repositories during each release; do not treat this date as a guaranteed production commit.

## 2. Ownership and architecture

- **ERP:** record, company, workflow module/levels, current assignment, business validation, Approve/Reject execution, transaction and execution receipt.
- **User Directory:** current identity, active/ban state, contact and applicable access evidence. It is not interchangeable with a local ERP user table.
- **Gateway:** authenticated event intake, actor bindings, immutable task evidence, delivery, authenticated inbound decisions, callback outbox, notifications, audit and reconciliation.
- **Meta:** sender/WABA, approved templates, media upload, outgoing message acceptance and webhook transport.

Flow:

1. ERP commits a relevant change and queues a workflow trigger **after commit**.
2. ERP job posts record identity to gateway; gateway durably accepts it.
3. Gateway worker resolves live ERP data, pending step and Directory recipients.
4. Gateway creates one task per authorized actor, scoped by system/company/record/type/step/version.
5. Worker revalidates workflow, prepares PDF/template, revalidates after document I/O, sends.
6. Meta sends a button or contextual text reply to the gateway webhook.
7. Gateway verifies signature, sender routing, actor/task state and live workflow; accepts ONE decision and queues a signed callback atomically.
8. ERP locks/revalidates the record, executes its existing action and commits a durable receipt.
9. Gateway accepts the matching execution result and sends a result confirmation.
10. ERP's after-commit trigger lets gateway reconcile sibling tasks / resolve the next step.

A gateway decision is an action at the assigned step. It must not claim the entire approval chain is complete unless ERP actually reports that.

## 3. Actual HTTP contract

Gateway routes are **not** prefixed with `/api/v1`.

| Method | Route | Authentication / purpose |
|---|---|---|
| GET | `/health` | Public liveness; does not prove all integrations work |
| GET | `/webhook/whatsapp` | Meta verification challenge and verify token |
| POST | `/webhook/whatsapp` | `X-Hub-Signature-256` against original body bytes |
| POST | `/api/workflow-events` | Bearer integration key; recommended ERP trigger path |
| POST | `/api/approval-requests` | Bearer key; direct actor task intake, still live-authorized |
| GET | `/api/approval-requests/{gateway_reference}` | Bearer key; same-system ownership enforced |
| POST | `/api/approval-requests/{gateway_reference}/cancel` | Bearer key; pending tasks only |

### Workflow event (recommended)

Exactly these four fields are required. `event_id` is a hyphenated UUID. Retain the same ID on transport retries; a new committed workflow change gets a new ID.

```http
POST /api/workflow-events
Authorization: Bearer <registered ERP gateway key>
Content-Type: application/json
Accept: application/json
```

```json
{
  "event_id": "a6b7b955-7531-4f78-abcb-f9329a2beba6",
  "source_system": "peta",
  "request_type": "requisition_approval",
  "reference_id": "282"
}
```

New event: HTTP 202. Identical replay: HTTP 200.

```json
{"accepted":true,"event_id":"a6b7b955-7531-4f78-abcb-f9329a2beba6"}
```

Same event ID with different content: 409. Invalid key: 401. Source-system mismatch: 403. The ERP does not choose recipients, phones or callback destinations in this envelope. Acceptance proves persistence, not message delivery or ERP execution.

### Direct task creation

Supported required fields: `company`, `request_type`, `reference_id`, `message`, `step_id`, `workflow_version`, `actor_directory_uuid`. `payload` defaults to an object. Optional accepted fields include `source_system`, `amount`, `callback_url`, `idempotency_key`; the authenticated client remains authoritative for ownership and callback registration. Do not use body-provided routing as authority. Prefer workflow events for PETA, since its adapter builds the snapshot and version.

```json
{
  "source_system": "peta",
  "company": "1",
  "request_type": "requisition_approval",
  "reference_id": "282",
  "message": "Review this requisition.",
  "step_id": "1",
  "workflow_version": "<adapter-generated record digest>",
  "actor_directory_uuid": "<verified Directory UUID>",
  "payload": {"record_snapshot": "<actual object, not this placeholder>"}
}
```

Response wraps `approval_request` and `created`; HTTP 201 new / 200 identical. This illustration is not executable until the real adapter-generated evidence and snapshot are supplied.

## 4. ERP callback contract

PETA receiver: `POST https://peta-api.iosuite.org/api/v1/whatsapp-approvals/callback`.

Headers:

```text
Content-Type: application/json
Accept: application/json
X-Approval-Gateway-Timestamp: <Unix seconds>
X-Approval-Gateway-Signature: <lowercase hex HMAC-SHA256>
Idempotency-Key: decision:<gateway_reference_id>
```

Signature:

```text
HMAC-SHA256(callback_secret, ASCII(timestamp) + '.' + exact_raw_body)
```

There is **no `sha256=` prefix** on this callback signature. That differs from the Meta webhook signature. Each retry gets a fresh timestamp/signature but retains the original event ID and payload bytes.

```json
{
  "event_id": "decision:AGR-EXAMPLE123",
  "gateway_reference_id": "AGR-EXAMPLE123",
  "reference_id": "282",
  "source_system": "peta",
  "company": "1",
  "request_type": "requisition_approval",
  "step_id": "1",
  "workflow_version": "<record digest>",
  "status": "approved",
  "responded_at": "2026-10-09T14:13:00+00:00",
  "channel": "whatsapp",
  "actor": {
    "directory_uuid": "<UUID>", "user_id": "152",
    "name": "john.john", "phone": "<international digits>"
  },
  "evidence": {
    "message_id": "wamid.<incoming>",
    "original_reply": "approve_AGR-EXAMPLE123",
    "context_id": "wamid.<outgoing>"
  },
  "payload": {
    "record_snapshot": {},
    "_gateway_workflow": {
      "module_id": "1", "step_id": "1", "record_digest": "<record digest>"
    }
  }
}
```

Required success confirmation is **HTTP 200**, matching `event_id`, and `execution_status` of `applied` or `rejected`:

```json
{"event_id":"decision:AGR-EXAMPLE123","execution_status":"applied"}
```

Business denial may be acknowledged with HTTP 200 and `execution_status: rejected`, optionally `reason`/`failed_checks`. It means the ERP **did not apply** the decision, not that the user clicked Reject. A successfully executed Reject still returns `execution_status: applied`.

HTTP 2xx without that matching contract is not proof of execution. Gateway retries connection errors, 429 and 5xx up to its limit; other HTTP failures are terminal unless deliberately recovered. Lost responses are recovered by replaying the SAME event and returning the stored ERP receipt.

### Laravel callback implementation rules

1. Require a configured signing secret; do not allow unsigned live callbacks when it is empty.
2. Verify exact raw bytes in constant time; enforce timestamp age/skew. Gateway includes a Python verifier with 300-second age and 30-second future skew; the ERP must enforce equivalent bounds itself.
3. Decode `$request->getContent()` and validate with `Validator::make($rawPayload, ...)`. Laravel middleware transformations must not change the signed snapshot before comparison.
4. Lock the requisition row and look up `approval_gateway_receipts` by system/event ID inside a transaction.
5. Existing receipt: verify payload hash and return the persisted response. Changed body for the same event: conflict.
6. Re-read details/chain, company, lifecycle, active pending step, assigned actor, module and immutable snapshot.
7. Call the existing ERP approval/rejection behavior only when checks pass. Do not directly invent status changes to bypass chain rules.
8. Commit business changes and receipt atomically. Dispatch the next workflow trigger after commit.

The exact final PETA PHP file should be reviewed against this contract during release; its earlier pasted controller contained a fail-open empty-secret path. Do not copy that earlier version into another ERP.

## 5. PETA record, identity and workflow evidence

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

## 6. PDF and templates

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

## 7. State model and persistence

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

## 8. Duplicate clicks and confirmations

One accepted decision atomically records the inbound message, changes task status, queues callback and receipt confirmation. Repeated delivery of the same inbound message is deduplicated. Another click on a completed task cannot generate a second ERP callback.

Confirmation wording is English and uses the verified snapshot company name; fallback is `Company <id>` if name/scope cannot be established.

- Receipt: decision received, processing underway.
- Applied result: decision applied at the assigned step.
- ERP denial: decision not applied; review in the company system.
- Opposite action: names attempted action and prior recorded/applied action; does not reverse the first decision.

WhatsApp visual button greying is not an authorization mechanism. Backend state is authoritative. Repeat-action wording distinguishes pending execution from applied execution.

Notifications use a conservative 23h55m window from verified user interaction. Old tasks are not automatically backfilled. Interrupted/ambiguous sends are marked unknown and not blindly resent. Notification failures do not undo ERP action. Worker summary counters do not include a separate notification count.

## 9. Deployment configuration

### Observed hosting

| Component | Location |
|---|---|
| Gateway code | CONTABO `/opt/whatsap-approval` (existing spelling retained) |
| Gateway public URL | `https://whatsapp-decision.kilinditraders.co.tz` |
| Gateway listener | `127.0.0.1:8088` on CONTABO |
| Database | `/var/lib/whatsap-approval/approval_gateway.sqlite3` |
| Logs | `/var/log/whatsap-approval/approval_gateway.log` and systemd journal |
| Adapter config | `/etc/whatsap-approval/integrations.json` |
| Runtime user | approval-gateway |
| PETA code | `/home/iosuite/public_html/peta-api/peta` |
| PETA PHP | `/opt/cpanel/ea-php83/root/usr/bin/php` |
| PETA API | `https://peta-api.iosuite.org/api/v1` |

Apache forwards public requests to loopback on the **same CONTABO server**. PETA on another host must use the public HTTPS gateway URL; its `127.0.0.1` points to the PETA host, not CONTABO.

### Gateway configuration example (placeholders, not secrets)

```dotenv
APP_ENV=production
APP_HOST=127.0.0.1
APP_PORT=8088
PUBLIC_BASE_URL=https://whatsapp-decision.kilinditraders.co.tz
DATABASE_PATH=/var/lib/whatsap-approval/approval_gateway.sqlite3
LOG_PATH=/var/log/whatsap-approval/approval_gateway.log
INTEGRATIONS_CONFIG_PATH=/etc/whatsap-approval/integrations.json
DEFAULT_EXPIRY_HOURS=24
CALLBACK_TIMEOUT_SECONDS=10
DRY_RUN_WHATSAPP=false
META_VERIFY_TOKEN=<verification challenge secret>
META_APP_SECRET=<owning app secret>
WHATSAPP_ACCESS_TOKEN=<token authorized for the selected sender>
WHATSAPP_PHONE_NUMBER_ID=<selected sender Phone Number ID>
WHATSAPP_API_VERSION=<configured supported Graph version>
WHATSAPP_TEMPLATE_NAME=<approved supported template in the same WABA>
WHATSAPP_TEMPLATE_LANGUAGE=en
PETA_API_BASE_URL=https://peta-api.iosuite.org/api/v1
PETA_GATEWAY_API_KEY=<same key configured by PETA document endpoint>
USER_DIRECTORY_ACCESS_TOKEN=<valid Directory service credential>
```

This is a target production example, not evidence that APP_ENV=production or the intended real sender is deployed. The last observed live setup had APP_ENV testing; review configuration before promotion.

Precedence: process environment > project `.env` > project `config.env`. Duplicate keys within files keep the first occurrence in this parser. Edit existing keys; do not append conflicting duplicates. A running process retains settings loaded at startup. After token/config changes, restart **server AND worker**; otherwise one can use stale credentials while manual diagnostics pass.

```json
{
  "systems": {
    "peta": {
      "enabled": true,
      "factory": "peta:build_adapter",
      "options": {
        "api_base_url_env": "PETA_API_BASE_URL",
        "access_token_env": null,
        "directory_base_url": "https://user-directory.abooderp.com/api/v1",
        "directory_access_token_env": "USER_DIRECTORY_ACCESS_TOKEN",
        "local_calling_code": "255",
        "allow_local_http": false,
        "document_access_token_env": "PETA_GATEWAY_API_KEY"
      }
    }
  }
}
```

`access_token_env: null` reflects the observed PETA details/chain endpoints; it is not a recommendation to make other ERPs public. Authenticate new ERP read endpoints according to their own policy.

### PETA environment

```dotenv
APPROVAL_GATEWAY_ENABLED=true
APPROVAL_GATEWAY_SYSTEM=peta
APPROVAL_GATEWAY_URL=https://whatsapp-decision.kilinditraders.co.tz
APPROVAL_GATEWAY_API_KEY=<registered gateway API key>
APPROVAL_GATEWAY_CALLBACK_SECRET=<registered integration signing secret>
DB_QUEUE_RETRY_AFTER=180
```

Do not confuse API key, callback secret, Directory token, Meta token, Meta app secret and Meta verify token. Their owners and purposes differ. Keep secret values out of commits, shell command arguments, screenshots and diagnostics.

## 10. Long-running processes and proxy

Gateway services: `approval-gateway` (server), `approval-gateway-worker` (worker).

Both use WorkingDirectory `/opt/whatsap-approval`, User/Group approval-gateway, Restart on-failure, UMask 0027, and writable paths limited to the gateway data/log directories. Worker command:

```text
/opt/whatsap-approval/.venv/bin/python -m approval_gateway.worker --loop --interval 5
```

Worker uses an exclusive file lock for batches. Do not run competing manual workers outside that control. `--interval` is validated from 1 to 300 seconds.

Apache HTTPS virtual host must proxy these paths (existing unrelated virtual hosts stay intact):

```apache
ProxyRequests Off
ProxyPass "/webhook/whatsapp" "http://127.0.0.1:8088/webhook/whatsapp"
ProxyPassReverse "/webhook/whatsapp" "http://127.0.0.1:8088/webhook/whatsapp"
ProxyPass "/health" "http://127.0.0.1:8088/health"
ProxyPassReverse "/health" "http://127.0.0.1:8088/health"
ProxyPass "/api/workflow-events" "http://127.0.0.1:8088/api/workflow-events"
ProxyPassReverse "/api/workflow-events" "http://127.0.0.1:8088/api/workflow-events"
```

The observed public deployment only needs the workflow trigger endpoint for ERP intake. If direct request/status/cancel APIs must be publicly accessible, proxy them explicitly; do not assume they are already exposed.

PETA cPanel cron, every minute, scoped only to its approval queue:

```cron
* * * * * cd /home/iosuite/public_html/peta-api/peta && /bin/flock -n storage/framework/peta-approval-gateway.lock /opt/cpanel/ea-php83/root/usr/bin/php artisan queue:work database --queue=approval-gateway --sleep=2 --tries=5 --timeout=90 --max-time=55 --stop-when-empty --no-interaction >> storage/logs/approval-gateway-queue.log 2>&1
```

Existing ABS/other crons remain unchanged. retry_after 180 is greater than worker/job timeout 90. max-time is checked between jobs; flock prevents overlapping invocations. Queue restart broadcasts a restart signal; it does not create a worker or cron.

## 11. Registering a new ERP

### Required onboarding worksheet

Record system identifier; company scopes; API base; actual record identifier; request type; module and step database IDs; ordering and lifecycle rules; identity mapping; assigned users; action policy; snapshot contents/version algorithm; PDF source; callback route; credential owners; queue connection; expected approve/reject effects and evidence.

### Adapter interface

Create trusted code under `approval_gateway/integrations/<erp>.py`. Register it in integrations.json with factory `<erp>:build_adapter` and environment variable references. Bootstrap publishes registry only after ALL enabled adapters initialize.

Required by registry:

```python
class ErpWorkflowGuard:
    def authorize(self, request):
        # Fetch current record and assignment; fail closed on bad/unavailable evidence.
        # Return WorkflowActor(directory_uuid, source_user_id, phone_number,
        #                      module_id, step_id, record_digest).
        ...
```

For the asynchronous workflow-event path also implement:

```python
    def resolve_workflow_event(self, event):
        # Return tuple/list of candidate dictionaries; max 100.
        # Resolve company, step, snapshot, version and recipients from live ERP.
        ...

    def prepare_delivery(self, settings, request):
        # Return validated template details and document media ID/filename.
        # Dry run must avoid real media uploads.
        ...
```

Candidate shape: source_system, request_type, reference_id, company, step_id, workflow_version, actor_directory_uuid, message, payload. Its digest must match authorize() evidence. Resolver must recheck assignment changes before returning; `()` only means a trustworthy no-current-work state, never a disguised network failure.

Raise `WorkflowDenied` for business prohibition; `IntegrationUnavailable` for inability to obtain trustworthy data. Do not convert an outage into an empty workflow, as reconciliation could cancel valid tasks.

### Registration and actor bindings

Use repository `create_api_client` / `upsert_approver` for deliberate secure operator provisioning. `seed.py` is **local/testing dry-run only**; do not disable production checks merely to run it live. Gateway stores API key hashes, so an existing plaintext key cannot be recovered from its hash; rotation must be coordinated with the ERP. Callback secret is registered separately.

Inspect CLI syntax without mutation:

```bash
.venv/bin/python -m approval_gateway.register --help
.venv/bin/python -m approval_gateway.bind_actor --help
```

Register callback (hidden prompt for secret; mutating provisioning command):

```bash
.venv/bin/python -m approval_gateway.register \
  --system <erp> --callback-url https://<erp-host>/<callback-route>
```

Register operator-verified binding:

```bash
.venv/bin/python -m approval_gateway.bind_actor \
  --system <erp> --company <company-id> \
  --directory-uuid <verified-uuid> --approver-id <gateway-approver-id> \
  --source-user-id <erp-user-id>
```

Registration does not prove phone ownership or grant ERP approval permissions. Re-registration does not silently change/re-enable conflicting bindings or callback settings. Plan mapping/key/URL rotation explicitly with pending tasks in view.

### ERP implementation sequence

1. Implement authenticated record/chain/PDF reads using actual system rules.
2. Implement signed callback + atomic business action/receipt.
3. Trigger gateway after committed create/submit, relevant workflow changes and decisions.
4. Implement adapter and scoped bindings; test resolver + authorize without sending.
5. Validate template fields in dry run.
6. Validate PDF endpoint/upload separately.
7. Send one intended actor task, verify inbound decision, execution receipt and ERP record.
8. Verify opposite/repeated clicks, stale tasks, outages and reconciliation.
9. Enable background queue/worker; monitor automatic new-record flow.

Payment Advice will require its actual controllers/models/chain and action semantics. Do not route it through Requisition handlers or reuse requisition snapshot/notification labels blindly. Current confirmation wording says “requisition”; generalize it deliberately when adding another request type. Template selection is a global setting in this baseline, not a per-ERP/per-task registry.

## 12. Verification commands (read-only unless labelled)

Gateway config (no secret output):

```bash
cd /opt/whatsap-approval
sudo -u approval-gateway .venv/bin/python - <<'PY'
from approval_gateway.config import load_settings
from approval_gateway.integrations.bootstrap import bootstrap_integrations
s = load_settings()
print('Adapters:', bootstrap_integrations(s))
print('Dry run:', s.dry_run_whatsapp)
print('Phone Number ID:', s.whatsapp_phone_number_id)
print('Template:', s.whatsapp_template_name)
print('Language:', s.whatsapp_template_language)
PY
```

Recent scoped state (change reference to the intended record):

```bash
sudo -u approval-gateway .venv/bin/python - <<'PY'
from approval_gateway.config import load_settings
from approval_gateway.db import connection
with connection(load_settings()) as conn:
    for row in conn.execute('SELECT id, reference_id, status, attempts, last_error FROM workflow_events ORDER BY id DESC LIMIT 5'):
        print('Event:', dict(row))
    for row in conn.execute('SELECT id, actor_user_id, status, delivery_status, delivery_attempts, delivery_error, execution_status FROM approval_requests WHERE source_system=? AND source_reference_id=? ORDER BY id', ('peta', '282')):
        print('Task:', dict(row))
PY
```

Services, journal, public health:

```bash
systemctl is-active approval-gateway approval-gateway-worker
systemctl is-enabled approval-gateway approval-gateway-worker
journalctl -u approval-gateway -n 80 --no-pager
journalctl -u approval-gateway-worker -n 40 --no-pager
curl --max-time 15 -sS https://whatsapp-decision.kilinditraders.co.tz/health
```

After changing gateway configuration (mutating process restart):

```bash
sudo systemctl restart approval-gateway approval-gateway-worker
```

PETA inspection:

```bash
cd /home/iosuite/public_html/peta-api/peta
/opt/cpanel/ea-php83/root/usr/bin/php artisan route:list --path=whatsapp-approvals
/opt/cpanel/ea-php83/root/usr/bin/php artisan migrate:status
/opt/cpanel/ea-php83/root/usr/bin/php artisan queue:failed
tail -n 30 storage/logs/approval-gateway-queue.log
```

After PETA env/code deployment, clear its configuration and restart its queue processes using the release procedure. Do not clear/delete receipt tables to get a new action through.

## 13. Troubleshooting learned during integration

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

## 14. Acceptance tests and release gate

Run local tests in an isolated development checkout, not against production data:

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests -p 'test_document.py' -v
python3 -m unittest discover -s tests -p 'test_workflow_integration.py' -v
```

Historical results: 53 baseline, 58 receipt/result confirmation tests, 60 company/English tests, 63 repeat-action tests passed in the documented patch progression. Test counts depend on checkout version; they are not a fresh test run of today's live host.

Required new-ERP cases: authenticated event replay/conflict; cross-system/company denial; no configured adapter; missing/revoked binding; wrong phone; inactive/banned Directory identity; explicit ID mapping; stale record/step/assignment; changing ERP during PDF I/O; partial candidate failure rollback; duplicate/opposite clicks; lost callback response and receipt replay; exact signature/body; clean JSON; PDF print flag unchanged; same-WABA template; ambiguous send handling; worker lock and restart recovery; multi-level chain/sibling cancellation; correct financial presentation by currency.

Before release record both git commits, schema status, non-secret config, credential owner/expiry plan, service/cron activity, one end-to-end outcome, and rollback code/config procedure. Do not rename deployed folders/data paths while resolving unrelated integration errors. Preserve existing services and crons belonging to other applications.

## 15. Remaining production work / next integration

1. Establish durable Directory machine credential or documented refresh mechanism; current adapter reads static environment credentials and does not renew them.
2. Complete Meta system-user token provisioning and validate asset permissions/expiry policy for selected sender.
3. Validate real sender `1326471317224727`, owning WABA/template, app webhook subscription and detailed-template end-to-end delivery/action. Latest successful diagnostics still used the test sender.
4. Confirm APP_ENV production and synchronized server/worker config after controlled release.
5. Improve safe failure diagnostics/monitoring so operators do not repeatedly reset queues to discover generic errors.
6. Review PETA public read authentication, callback freshness/empty-secret handling, receipt migration uniqueness and removal of temporary diagnostics against the final deployed files.
7. Financial details/PDF consistency: existing details code originally calculated materials-based totals; account-only/discount/multi-currency behavior requires dedicated acceptance tests before generalizing.
8. Add Payment Advice through its real workflow contract; generalize confirmation labels and evaluate template routing rather than assuming the current requisition-only adapter supports it.

## 16. Code map

| File | Responsibility |
|---|---|
| config.py | Environment precedence, settings and production validation |
| server.py / meta.py | HTTP routing, signature verification, inbound extraction |
| workflow_events.py | Authenticated durable four-field trigger inbox |
| service.py | Task authorization/persistence, decision, callback payload, reconciliation |
| worker.py | Locked event/delivery/callback processing and retry classification |
| notifications.py | Receipt/result/repeat-action notifications |
| whatsapp.py / upload_document.py | Template/message and PDF media transport |
| integrations/bootstrap.py / registry.py | Configured trusted adapter factories |
| integrations/permissions.py | WorkflowActor/WorkflowGuard and shared permission contracts |
| integrations/directory_client.py | Bounded Directory HTTP client / normalization |
| integrations/peta.py | PETA record/chain/identity/digest/document/display adapter |
| db.py / upgrade.py | Gateway schema and idempotent migrations |
| register.py / bind_actor.py | Callback and operator-verified scoped identity provisioning |
| PETA NotifyApprovalGateway | After-commit ERP trigger job, dedicated queue |
| PETA WhatsAppApprovalCallbackController | Raw signed event validation / handler dispatch |
| PETA PetaRequisitionDecision | Locked revalidation, existing business action, execution receipt |
| PETA WhatsAppApprovalDocumentController | Authenticated PDF route, no print-side effect |

Maintain this guide with every contract/schema/template change. New ERP integration should start with sections 3–5 and 11, not historical troubleshooting commands or obsolete API proposals.
