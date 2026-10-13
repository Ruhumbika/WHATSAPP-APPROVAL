# WhatsApp menu and gateway admin — Phase 4

Base: gateway Phase 3 approval inbox (deployed commit 865296d). PETA Phase 3 must already be deployed. This patch changes the gateway only.

## User experience

All user-facing messages are English.

- MENU, HI, HELLO or START opens Pending approvals / Find a request / Help & more.
- Unknown text outside an active input step opens the main menu.
- HELP, GUIDE, MANUAL and USER MANUAL work globally.
- Help & more opens User guide and Contact support. Only admins see Admin controls.
- Pending approvals has selectable request rows, Next page and Main menu. It preserves live Directory verification and ERP access checks.
- Find a request prompts for REQ 312 or PA 108; use a real ID. Invalid input repeats guidance.
- PENDING, REQ, PA and STATUS commands still work.
- During a rejection, HELP shows contextual guidance without submitting anything.
- MENU during rejection offers Continue rejection or Cancel rejection. Cancellation keeps the approval pending.
- A reason entered while a reason is requested still submits the rejection immediately. This release does not add a second rejection confirmation.
- Messages replying to a contextual help prompt remain bound to that rejection task.
- Buttons/lists are replies to inbound WhatsApp messages, with a conservative 23h55m reply window. Existing proactive approved templates remain unchanged.
- Public help is available without an ERP binding. Reading requests or deciding still requires the existing authorization.
- Images/audio and unsupported message types are not interpreted by this menu.

## Admin configuration

Add to the gateway's own .env (international digits only, no plus or spaces inside a number):

    WHATSAPP_ADMIN_PHONES=255787550399

Multiple admins: comma-separated numbers, up to 20. Empty/unset means no WhatsApp admins.
Do not commit your .env.

The environment allowlist is the only admin authority. No admin phone registry is copied into SQLite.
Restart both gateway services after adding/removing numbers. Both processes must load the same environment.
Old menu clicks and queued actions recheck the current loaded allowlist.
A number's admin access follows the configured phone, so remove old numbers when changing ownership.

Send ADMIN from an allowed number. Admin menus provide:

- Reminder settings: pause/resume, interval (1/4/6/12/24 hours), maximum (0/1/3/5/10).
- Queue status: aggregated approval, notification and inbox/menu counts.
- Help content: edit welcome/support text (3–300 characters).
- Recent changes: latest five audit entries.

Every setting mutation has a preview and Confirm change / Cancel.
Confirmations reject stale revisions and repeated delivery does not reapply changes.
Settings apply gateway-wide, across companies, and are explicitly labelled in the menu.
Admin authority does NOT grant approval or request access.

SQLite stores settings, revision and audit evidence (actor, timestamp, setting, old/new values, inbound ID).
Stored reminder overrides take precedence over .env reminder defaults.
Pause only suppresses action reminders, not approval requests, progress or completed documents.
A reminder already accepted by Meta cannot be recalled.
Resume respects current stage/dedup rules and does not produce a catch-up burst.
WORKFLOW_NOTIFICATIONS_ENABLED remains the environment master switch.

Queue counts are operational summaries. "Sent" means provider acceptance, not confirmed delivery/read.
Per-message delivery diagnostics, retries from admin, quiet hours, digest and MY REQUESTS are not part of this patch.

## Laptop apply and test

Run inside the WHATSAP CONVERSATION repository after extracting the bundle to Downloads:

    git apply --check ~/Downloads/whatsapp-menu-admin-phase4/gateway-menu-admin-phase4.patch &&
    git apply ~/Downloads/whatsapp-menu-admin-phase4/gateway-menu-admin-phase4.patch &&
    python3 -m unittest discover -s tests -q &&
    git diff --check

Expected: 177 passing tests. Deliberate 403/503/context-unavailable fixture logs may appear with OK.
If --check fails, stop and provide git status --short and the exact error. Do not force or use --reject.

New SQLite schema is additive and created by the existing startup migration.
No PETA migration, Composer change or frontend deployment is required.

## Deploy after laptop verification

Commit only the patch's source/tests/documentation; exclude .env, editor folders and backup files.
Push through the team's normal main-branch workflow.
On the gateway host:

    cd /opt/whatsap-approval &&
    git pull --ff-only &&
    sudo systemctl restart approval-gateway approval-gateway-worker &&
    sudo systemctl is-active approval-gateway approval-gateway-worker

Set the allowlist in the live gateway environment before restarting.

## Live acceptance checklist

1. Send HELLO and unknown text: main menu appears; text/buttons are English.
2. Select Pending approvals, a request, and Next page where available.
3. Find an accessible REQ/PA; inaccessible requests reveal no details.
4. HELP opens guidance; during rejection it must not submit a decision.
5. MENU during rejection -> Continue keeps context; Cancel leaves approval pending.
6. From an admin: ADMIN -> Reminder settings -> Pause -> Cancel makes no change.
7. Repeat with Confirm; check audit and absence of new action reminders. Resume afterwards.
8. Edit support details and verify Contact support shows the saved text.
9. From a nonadmin: ADMIN is denied; Help & more has no admin option.
10. Remove an admin from .env and restart both services: old admin buttons no longer work.
11. Normal approval, correction and completed PDF flows still work.

Automated tests use mocked provider/ERP responses. Real WhatsApp rendering and live delivery must be confirmed with this checklist.
