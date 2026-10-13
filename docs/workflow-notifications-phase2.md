# Workflow notifications — phase 2

Apply these patches locally on top of the successfully applied phase-1-v2 patch.
Do not apply phase 1 again. Do not change production code directly.

## Included behavior

- New successfully resolved PETA workflow events enroll the request for automatic notifications.
- The persisted request creator receives progress updates when workflow state changes.
- Completion sends workflow_completed_document instead of a redundant progress message.
- Reviewers receive workflow_action_reminder only for their current step and only if a matching
  approval message was delivered and the task remains current and unexpired.
- Defaults: reminder intervals of 6 hours, at most 3 scheduled intervals per step (6/12/18 hours).
  The timer starts when the gateway first observes the step. ERP waiting_since remains
  'Not recorded' when PETA does not provide a reliable timestamp.
- Missed reminder intervals do not generate a burst of catch-up messages.
- View progress obtains a fresh authorized journey. View request obtains the current review PDF;
  decisions still use Approve/Reject on the original approval message. Neither view button changes ERP state.
- Directory remains the authority for current phone numbers; existing disabled/conflicting bindings are respected.
- Retries and deduplication are durable. Unknown provider acceptance is retained for review, not resent automatically.
- Request creators do not need to be provisioned as approvers to receive their updates or use View progress.
- Requisition completion preserves its existing PDF and appends recorded approval actions from the current cycle.
  Payment advice completion includes reviewed amounts, payment parties and actual approval actors/actions.
  No handwritten signatures are fabricated. No payment or posting is implied.

## Laptop: gateway

From WHATSAP CONVERSATION:

```bash
git apply --check ~/Downloads/workflow-notifications-phase2/gateway-workflow-notifications-phase2.patch &&
git apply ~/Downloads/workflow-notifications-phase2/gateway-workflow-notifications-phase2.patch &&
python3 -m unittest discover -s tests -q &&
git diff --check
```

Gateway verification performed in the preparation environment: 137 tests passed.

## Laptop: PETA backend

From ABOOD/peta:

```bash
git apply --check ~/Downloads/workflow-notifications-phase2/peta-workflow-notifications-phase2.patch &&
git apply ~/Downloads/workflow-notifications-phase2/peta-workflow-notifications-phase2.patch &&
php artisan test --filter='WorkflowProgressTest|RequisitionApprovalStatusServiceTest|RequisitionWhatsAppIntegrationTest|PaymentAdviceWhatsAppIntegrationTest' &&
git diff --check
```

PHP/Laravel tests have NOT been executed in the preparation environment because it has no PHP runtime.
The added feature tests exercise context authentication, authoritative creator/company selection,
full-chain completion, revision/company checks, and real PDF generation. Run them before deploying.
Inspect one completed requisition and payment-advice PDF on your test environment as a final visual check.

## Configuration and deployment order after tests pass

1. Commit and deploy PETA through the existing GitHub workflow first. Refresh its route cache if used.
2. Commit and deploy gateway through GitHub. Gateway startup creates workflow_watches and
   workflow_notifications tables automatically; no Laravel database migration is needed.
3. Enable the feature in the gateway's operator-managed .env (not in source control):

```dotenv
WORKFLOW_NOTIFICATIONS_ENABLED=true
WORKFLOW_REMINDER_HOURS=6
WORKFLOW_REMINDER_LIMIT=3
```

WHATSAPP_TEMPLATE_LANGUAGE must match the approved English language code of all three templates.
Do not replace WHATSAPP_TEMPLATE_NAME: that setting still controls the original approval request template.
Restart approval-gateway and approval-gateway-worker after changing settings.
Choose reminder intervals shorter than your approval task expiry; expired tasks are never reminded.
Set WORKFLOW_REMINDER_LIMIT=0 to disable reminders while retaining progress/completion messages.

Existing historical events are not bulk replayed and existing approvals are not resent.
Use a new test request after activation, or let an existing request's next genuine workflow event enroll it.
For a short reminder smoke test, WORKFLOW_REMINDER_HOURS=1 is supported; restore 6 afterwards.
The feature defaults to disabled until both backends are ready.

## Live smoke test after deployment

- Submit a designated test requisition and confirm the creator gets progress.
- Complete a non-final action: creator gets updated progress; next reviewer receives their approval request.
- View progress must show the current step and actual recorded previous actor.
- Leave a delivered test task pending for the configured interval: only current reviewers get a reminder.
- View request returns its PDF. Use the original approval message to act.
- Complete the final action: creator receives one completed PDF. Verify actors and final reviewed amounts.
- Repeat with payment advice. Test correction/resubmit to confirm a new cycle and no old-stage reminders.

Inspect failures without printing credentials:

```bash
sudo journalctl -u approval-gateway-worker --since '15 minutes ago' --no-pager | tail -n 80
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('file:/var/lib/whatsap-approval/approval_gateway.sqlite3?mode=ro', uri=True)
for row in con.execute('SELECT id,kind,status,attempts,last_error FROM workflow_notifications ORDER BY id DESC LIMIT 20'):
    print(row)
con.close()
PY
```

Provider acknowledgement is not proof of handset delivery. Confirm receipt in WhatsApp during the smoke test.
Do not reset 'unknown' notifications without checking provider acceptance.

PDF import implementation reference: https://mpdf.github.io/reference/mpdf-functions/setsourcefile-v8.html
