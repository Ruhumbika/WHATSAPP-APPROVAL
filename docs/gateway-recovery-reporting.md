# Expired approval recovery and admin reporting

## Behavior

ERP events can replace an expired task only after the adapter resolves and verifies the current reviewer, step, snapshot and Directory identity. New AGR reference/expiry; old task identifiers and decisions stay historical. Original buttons are never revived.

REQ/PA document browsing can recover an existing expired assignment. Search does not create tasks with no previous matching assignment. A recovered inbox document satisfies the new task delivery without a second initial approval message.

Current PETA workflow watches schedule one durable recovery event per expired generation when ERP still lists the reviewer. Recovery can take multiple worker passes. Startup enrolls existing pending and expired numeric PETA references. Task expiry duration is unchanged.

Unknown delivery outcomes are not automatically renewed/retried. Changed identity, revoked bindings and mismatched workflow evidence remain denied.

## WhatsApp admin

Authority remains WHATSAPP_ADMIN_PHONES. No new environment settings.

ADMIN > Trace and resolve > send:
    peta REQ 301
    peta PA 108

Trace shows recent task states, reviewers, expiry in UTC, delivery errors, callback attempts/errors, workflow-event state and read-only current ERP assignments.

Reconcile request > review > Confirm reconcile queues a record-only event. Worker revalidates ERP/Directory, retires obsolete pending assignments and can issue replacement tasks. Repeated clicks on one confirmation share one operation identity. This never approves/rejects a record. Trace result shows completion/failure evidence.

Retry delivery > task ID > Confirm retry accepts only current unexpired tasks with confirmed Meta HTTP 4xx rejection and no accepted message ID. Live authorization and binding are checked again. Unknown, sent, expired and blocked deliveries are refused. Retry history remains in audit.

ADMIN > Message history provides pagination and full per-message content/status details. Stored historical queue records are included. Queue totals are historical jobs/deliveries, not a live ERP actionable inbox.

## History and CSV report

Real outgoing messages are journaled before transport with recipient, UTC timestamp, message kind, readable content/template body values and available system/request/company linkage. Interactive action tokens, bearer tokens and document download URLs are excluded. Templates retain name/body values; the report does not reconstruct Meta's full rendered template.

Accepted means Meta acknowledged a send. Delivered/read/failed require received Meta receipts. Ambiguous/interrupted sends remain unknown. Typing/read-receipt requests are excluded. Dry-run transport is not a real sent message; historical simulated queues retain their label.

Old records may have metadata only. Decision/conversation text already stored in old queues is included. Missing historical content cannot be reconstructed; some old timestamps indicate queue creation rather than provider acceptance.

CSV export runs on the VPS; Excel opens this CSV. Admin > Message history > Report export help explains the operator step. Native XLSX/PDF export and chat-delivered reports are not included in this release.

Example (use a configured admin number):
    cd /opt/whatsap-approval
    .venv/bin/python -m approval_gateway.message_history --admin-phone 255787550399 --since 2026-10-01 --until 2026-10-15 --output /tmp/whatsapp-message-report.csv

Optional filters: --recipient 255XXXXXXXXX, --system peta, --reference 301, --status read.
Dates are UTC and inclusive. System/reference filters include only known linkage; general menus may have no request. CSV uses one database read snapshot and neutralizes spreadsheet formula prefixes. Restrict exported content to authorized staff.

## Laptop apply

Inside the gateway repository:
    git apply --check ~/Downloads/gateway-recovery-reporting/gateway-recovery-reporting.patch &&
    git apply ~/Downloads/gateway-recovery-reporting/gateway-recovery-reporting.patch &&
    python3 -m unittest discover -s tests -q &&
    git diff --check

Based on the exact uploaded gateway-current-source.tar.gz. Do not force onto another tree. No PETA/frontend patch, new credentials or manual database edits are required.

Review git status, stage the patch's named files, commit and push normally. Do not stage .env or database files.

## VPS deployment

Take your normal consistent SQLite backup before additive migration; do not copy only an active database file during writes.

    cd /opt/whatsap-approval &&
    git pull --ff-only origin main &&
    sudo systemctl restart approval-gateway approval-gateway-worker &&
    sudo systemctl is-active approval-gateway approval-gateway-worker

Startup adds the journal schema and enrolls expired watches; existing tasks/history remain.

## Live acceptance

1. ADMIN > Trace and resolve > peta REQ 301. Confirm current ERP reviewers.
2. Reconcile request > Confirm reconcile; Trace result after worker processing. Expect fresh pending tasks with old expired tasks retained.
3. Assigned user searches REQ 301 and opens document; current Approve/Reject should appear.
4. Old buttons must not execute. Removed reviewers must receive no fresh action.
5. Two reviewers: one completes stage; obsolete draft/task retires; a reviewer still required by ERP remains active.
6. Message history > View message; compare sends/receipts and Meta IDs; export CSV.
7. Remove admin from allowlist and restart: old confirmations must fail.
8. Use test records for approve/reject acceptance.

ERP/Directory unavailability never bypasses authorization; trace/events retain investigation evidence.
