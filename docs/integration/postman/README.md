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
