# WhatsApp Approval Gateway — Postman Setup

Import **all three files** from this directory:

1. `WhatsApp-Approval-Gateway.postman_collection.json` — API requests, examples and automated assertions
2. `environments/Local-Sandbox.postman_environment.json` — local development settings
3. `environments/Production-Template.postman_environment.json` — blank production template; **do not use on live approvals until approved**

In Postman, use **Import → Files** for the three files. Select **Approval Gateway — Local Sandbox** from the environment dropdown (top right). Under that environment, populate:

| Variable | Purpose |
| --- | --- |
| `baseUrl` | Running gateway base URL; default `http://127.0.0.1:8088` for local |
| `apiKey` | ERP-specific API client key; initially **empty**, must be securely provisioned |
| `sourceSystem` | Registered ERP system identifier, e.g. `ghrm` |
| `company` | Company scope in ERP; placeholder `14` is **not** a universal company ID |
| `requestType` | Workflow action, e.g. `leave_approval` |
| `referenceId` | ERP record primary reference |
| `stepId` | Verified current workflow step |
| `workflowVersion` | ERP verified record snapshot/digest |
| `actorDirectoryUuid` | Verified approver Directory UUID; example UUID is not a real employee |
| `idempotencyKey` | Stable per unique ERP task and version |
| `gatewayReferenceId` | Saved automatically by successful **02 — Create approval** |
| `workflowEventId` | Unique event UUID; only for ERPs with configured workflow-event resolvers |

### Normal workflow

- Select Local Sandbox; ensure the gateway and a **sandbox ERP verification endpoint and callback endpoint are configured**.
- Run **A / 01 — Health**, **A / 02 — Create approval**, **A / 03 — Get approval status**.
- Postman tests automatically check response codes, payload structure and store the new gateway reference in the selected environment.
- **B — Workflow Event** is optional and not supported by generic ERP mode.
- Run negative-security tests **only in sandbox**. Run cancellation only on disposable test approvals. Don't run the entire collection automatically against production.

### Secrets and safety

The environment JSON files deliberately do not contain real credentials. `apiKey` is declared a secret variable; enter the real value in Postman privately and avoid exporting it with values populated. The gateway's ERP verification token and callback HMAC secret are **operator-side secrets**, not values to place in the client Postman environment. Sharing the Postman file does not register a new ERP.

Validation:

```bash
python3 -m json.tool docs/integration/postman/WhatsApp-Approval-Gateway.postman_collection.json >/dev/null
python3 -m json.tool docs/integration/postman/environments/Local-Sandbox.postman_environment.json >/dev/null
python3 -m json.tool docs/integration/postman/environments/Production-Template.postman_environment.json >/dev/null
python3 -m unittest discover -s tests -q
```

## Complete endpoint contract: Gateway HTTP server

| Method | Endpoint | Authorization | Request Body | Success |
|---|---|---|---|---|
| GET | `{{baseUrl}}/health` | None | None | 200, `{"status":"ok","version":"2.2"}` on the current code |
| POST | `{{baseUrl}}/api/approval-requests` | `Bearer {{apiKey}}` | Universal JSON approval request (folder A/02) | 201 new / 200 duplicate, `{"approval_request":{...},"created":true/false}` |
| GET | `{{baseUrl}}/api/approval-requests/{{gatewayReferenceId}}` | `Bearer {{apiKey}}` | None | 200, `{"approval_request":{...}}` |
| POST | `{{baseUrl}}/api/approval-requests/{{gatewayReferenceId}}/cancel` | `Bearer {{apiKey}}` | None | 200, cancellation result; use only for disposable test tasks |
| POST | `{{baseUrl}}/api/workflow-events` | `Bearer {{apiKey}}` | Exactly `event_id,source_system,request_type,reference_id` | 202 new / 200 same event replay; **requires registered event resolver, not available for generic adapter** |
| GET | `{{baseUrl}}/webhook/whatsapp` | Meta verification token in query | None | Meta subscription challenge; **Meta-only, not ERP integration** |
| POST | `{{baseUrl}}/webhook/whatsapp` | Valid Meta `X-Hub-Signature-256` | Signed Meta webhook JSON | Provider acknowledgement; **Meta-only, not ERP integration** |

All authenticated JSON POST bodies use `Content-Type: application/json`. Each ERP API client is scoped to its registered `source_system`. No API exists to register a new ERP or change an actor through a public JSON payload.

## ERP-owned endpoints — requests sent TO the ERP backend, NOT Gateway routes

| Method | ERP Endpoint (example only) | Headers | Body | Expected reply |
|---|---|---|---|---|
| POST | `{{erpVerifyUrl}}` | `Authorization: Bearer {{erpVerifyToken}}`, `Content-Type: application/json` | `company, request_type, reference_id, step_id, actor_directory_uuid, workflow_version` | 200 with exact current actor/record verification fields, or 403 |
| POST | `{{erpCallbackUrl}}` | `Content-Type: application/json`, `X-Approval-Gateway-Timestamp`, `X-Approval-Gateway-Signature`, `Idempotency-Key` | Signed decision event, folder E/07 example | 200 with matching `event_id` and `execution_status=applied|rejected` |

The callback is emitted automatically by the gateway worker after an authorized WhatsApp decision. **Folder E/07 is a contract example only:** its sample timestamp and signature are intentionally blank. A raw/manual Postman request cannot validly impersonate a signed gateway callback. `erpVerifyToken` is an **operator-side secret**; only populate this for an isolated ERP developer sandbox; never export or distribute it. There is no generic event resolver for `POST /api/workflow-events`.

Local `http://127.0.0.1:8090/verify` and `/callback` are example addresses **not** running by default. Start an independently configured mock ERP or replace them with your ERP's actual sandbox URLs.

## Release readiness

This collection is a complete interface/contract template, **not a guarantee a new ERP works immediately with only the API key**. Register the ERP, provision its verification endpoint and signed callback, verify trusted approver identity and phone, and certify the flow using a sandbox ERP. No live PETA configuration is changed by importing the files.

## Developer handoff and ERP-specific examples

The send-ready document is [`../ERP-DEVELOPER-HANDOFF.md`](../ERP-DEVELOPER-HANDOFF.md). Folder **F — Example Payloads by ERP** contains five non-production templates: GHRM, ABS, CRM, MaCargo and Ticketing. The outer JSON field names remain fixed; business metadata varies under `payload`. Those examples are **not ready-to-run requests**: every ERP requires its own registered source-system key, verification endpoint, callback endpoint, and actual company/record/actor IDs. PETA retains its existing specialized adapter. Never run folder F in the collection runner or against production without explicit authorization.
