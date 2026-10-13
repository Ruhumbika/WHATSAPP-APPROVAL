# Approval inbox — phase 3

Apply on the laptop, test, then commit/push and deploy PETA before the gateway.
These patches match the two inbox-base archives supplied on 13 October 2026.
They preserve the `require $file` route-cache fix and existing approval templates.

## Included

- `PENDING`: up to five current assigned requisitions/payment advices, oldest-created first.
- `REQ 312`, `REQ-00312`, `PA 108`: authorized compact summary.
- `PENDING C1`: explicit company when a number belongs to several company bindings.
- `PENDING C1 NEXT 5`: continue using the exact command returned by the last page.
- `REQ 312 C1` / `PA 108 C1`: search within a selected company.
- Current, delivered, unexpired tasks whose full workflow evidence matches PETA open as a PDF with Approve/Reject buttons. Buttons use the same existing AGR task and decision/rejection flow. Original approval buttons remain valid subject to the existing checks; a decision from either surface prevents a duplicate execution.
- Creators and previous/assigned reviewers permitted by the existing progress endpoint can search. Only the current reviewer with an actionable WhatsApp task gets buttons.
- Durable, inbound-deduplicated reply queue, bounded retries, 23h55 reply window, unknown-acceptance protection and ten commands/minute/phone limit.
- English messages, purpose/requester/stage/amount summary; at most three currency totals shown with full details in the PDF.

## Scope and limits

No new Meta templates or template settings are needed. Inbox responses are initiated by the user's message and queued only within its response window. Existing notification templates/settings are unchanged.

Pending data comes from PETA, not just the gateway's delivery history. PETA inspects at most 100 company-scoped candidate records per request and returns at most five authorized results. An empty batch may have a Next command. No total pending count is invented. Pagination is a live offset: if assignments/records change between pages, refresh PENDING to restart. Order is record creation time, not an invented stage waiting time.

Users need an existing active gateway actor binding. This phase does not onboard arbitrary unbound creators. Directory is authoritative for current phones. Stored phones are lookup hints, verified live; discovery of changed phones is bounded to 20 active Directory identities in the selected scope. Larger unmatched scopes require a company qualifier or a binding refresh through the established verified workflow. Disabled/conflicting identities remain blocked.

No new approval tasks are created, no expired task is renewed, and no sent task is retargeted when a phone changes. If the current task does not match, the user gets a summary with a PETA instruction. Non-current reviewers get a summary, not a PDF/action button.

MY REQUESTS, quiet hours, reminder digests and delivery-monitoring UI are later phases.

## Laptop: PETA

From your PETA repository:

```bash
git apply --check ~/Downloads/approval-inbox-phase3/peta-approval-inbox.patch &&
git apply ~/Downloads/approval-inbox-phase3/peta-approval-inbox.patch &&
php artisan test --filter='WorkflowProgressTest|RequisitionApprovalStatusServiceTest|RequisitionWhatsAppIntegrationTest|PaymentAdviceWhatsAppIntegrationTest' &&
git diff --check
```

Eight new PHP feature tests were added to the existing isolated fixtures: authorization, current vs future reviewers, creator search, other-company/outsider denial, five-item pagination, assignment changes after approval, and draft removal. PHP and vendor dependencies are not available in the patch-building environment: these tests must pass on your laptop before deployment.

## Laptop: gateway

From your gateway repository:

```bash
git apply --check ~/Downloads/approval-inbox-phase3/gateway-approval-inbox.patch &&
git apply ~/Downloads/approval-inbox-phase3/gateway-approval-inbox.patch &&
python3 -m unittest discover -s tests -q &&
git diff --check
```

Gateway validation: full test suite, including queue deduplication, current Directory phone, disabled bindings, company scope, pagination bounds, source search, expiry, PDF preparation races, unknown acceptance, Meta payload parsing, real decision/rejection routing and stale assignment/snapshot checks.

## Deployment

1. Commit only intended files; push to main and synchronize your backend branch through your normal merge process.
2. PETA: pull main in the existing application directory; clear and rebuild route cache. Confirm `route:list --path=whatsapp-approvals/inbox` shows two routes and `route:list --path=basic-data` still shows the existing routes.
3. Gateway: pull main in `/opt/whatsap-approval`; restart `approval-gateway` and `approval-gateway-worker`; confirm both active. Startup automatically creates the `inbox_replies` SQLite table. No Laravel migration or new dependency is required.
4. Test PENDING from a known reviewer; search both REQ and PA; review the attached PDF; verify approval advances one step only and rejection still asks for mode and reason. Test creator search, next page, unauthorized search and an expired/changed task.
5. Confirm existing templates, automatic updates and completion PDFs still work.

Inbox queue diagnostics (read only):

```bash
python3 - <<'PY'
import sqlite3
with sqlite3.connect('file:/var/lib/whatsap-approval/approval_gateway.sqlite3?mode=ro', uri=True) as db:
    for row in db.execute('SELECT id,status,attempts,last_error FROM inbox_replies ORDER BY id DESC LIMIT 20'):
        print(row)
PY
```

`unknown` means provider acceptance cannot be confirmed; do not bulk requeue it. Inbound duplicates never create another reply job. User may issue a new command if they need a fresh view.
