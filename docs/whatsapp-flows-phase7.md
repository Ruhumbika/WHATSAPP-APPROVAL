# Phase 7 — Optional rejection form

Requires Phase 6 reviewer/service patch (202 tests). Gateway only; no PETA changes.
After this patch: 217 tests pass. Tests use mocked Meta/ERP/Directory calls.

## User experience

1. Start Reject on an authorized request using the existing buttons.
2. Send HELP or HEY, or open Main menu > Resume rejection.
3. When enabled, choose Open rejection form for that request.
4. Choose Return for correction or Reject completely, and enter a reason.
5. Review rejection returns a draft to chat. No ERP decision has been sent.
6. Use Submit rejection, Edit reason or Cancel rejection in chat.

The existing Continue rejection path is always available. The form starts blank;
an existing chat draft remains saved until a valid form completion replaces it.
Leaving the form does not cancel or submit the task. The User guide explains both paths.
Product messages and the form are English; request context is not branded to one ERP.

## Safety and operation

- Default OFF. Disabled gateways retain existing menus and rejection behavior.
- Launch follows live ERP authorization and uses the durable inbox queue/service window.
- Published mode only. Launch tokens bind to task, sender, draft fingerprint, sent message,
  and a maximum 15-minute expiry (shorter if task/session expires earlier).
- The existing signed Meta webhook handles nfm_reply before ordinary chat routing.
- Completion rechecks live ERP/Directory authority, then atomically saves the draft and
  consumes the token. It never creates an ERP callback.
- Final Submit rejection retains the existing live authorization and confirmation checks.
- Chat edits, cancel, resume or a newly opened form invalidate older forms.
- Retired/cancelled tasks, changed snapshots/phones and expired sessions cannot save a form.
- Duplicate inbound IDs are ignored; replays with new IDs cannot reuse consumed tokens.
- Provider acceptance with unknown delivery is not automatically resent. Use HELP to recover.
- Form completions require the original outbound message context. Confirm that the actual
  device webhook includes it during the pilot; missing/mismatched context fails safely.
- No encrypted data-exchange endpoint is needed for this static navigate/complete form.
  Form completion arrives on the existing signed message webhook.

## Apply locally

Extract the archive into Downloads. If Phase 6 has not been applied, first use its included
patch with --check, apply it, and run the suite (202 tests). Do not reapply Phase 6 if already present.

    git apply --check ~/Downloads/whatsapp-flows-phase7/gateway-reviewer-service-phase6.patch &&
    git apply ~/Downloads/whatsapp-flows-phase7/gateway-reviewer-service-phase6.patch &&
    python3 -m unittest discover -s tests -q &&
    git diff --check

Then apply Phase 7:

    git apply --check ~/Downloads/whatsapp-flows-phase7/gateway-flows-phase7.patch &&
    git apply ~/Downloads/whatsapp-flows-phase7/gateway-flows-phase7.patch &&
    python3 -m unittest discover -s tests -q &&
    git diff --check

Stop if --check fails; do not force the patch. Existing fixture warning logs are expected;
judge the test result by the final OK. No live deployment or publication was performed here.

## Meta readiness — what to do now

In WhatsApp Manager, select the same WABA that owns the gateway sending number, then Flows.
Create a draft, upload flows/rejection-review.json and inspect validation errors and Preview.
The JSON has local syntax/contract checks, but has NOT been validated or published by Meta.
Resolve any Meta validator errors before publication. A draft preview is not live readiness.
Keep the gateway feature OFF while doing this.

Read status using the existing gateway token (token is never printed):

    python3 -m approval_gateway.flow_readiness --flow-id YOUR_NUMERIC_FLOW_ID

Use the gateway virtualenv's Python if dependencies are installed there. This command makes
one read-only GET; it does not publish, send messages, change settings or start the worker.
It returns id, status, validation_errors and health_status. A 403 means check permissions,
Flow ownership and token access. Missing health fields are inconclusive, not a pass.
Share the status/errors and a screenshot of the Flow screen; never share your access token.

Before enabling: status PUBLISHED, no validation errors, health allows sending, and a
controlled device test verifies form rendering plus the nfm_reply webhook. Existing approved
message templates do not prove Flow readiness. This phase does not change their names.
No new proactive Flow template is introduced: forms launch in response to a user's menu action.

Once the published Flow is ready, on the gateway set:

    WHATSAPP_REJECTION_FLOW_ENABLED=true
    WHATSAPP_REJECTION_FLOW_ID=YOUR_NUMERIC_FLOW_ID

Replace the placeholder with the actual numeric ID. Do not put a Flow name here.
Pull tested commits through the normal workflow, then restart both services:

    sudo systemctl restart approval-gateway approval-gateway-worker &&
    sudo systemctl is-active approval-gateway approval-gateway-worker

To turn forms off, set WHATSAPP_REJECTION_FLOW_ENABLED=false and restart both services.
Do not change the approved template settings. Existing buttons remain available.

## Pilot acceptance

- Check Phase 6 first: two reviewers, one completes the stage, the other receives an update,
  its old rejection is closed, and a stage that still requires that reviewer remains pending.
- With Flow enabled, start a test rejection, HELP, Open rejection form, fill and Review rejection.
  Confirm the request is still pending until Submit rejection is explicitly clicked.
- Test correction and final rejection separately on authorized test records.
- Before submitting a form, let another reviewer finish the stage. The old form must fail safely.
- Open a form, edit the chat draft, then complete the older form: it must not overwrite the edit.
- Re-send the same webhook in a test harness: only one draft/review, no extra callback.
- Test HELP > Continue rejection on a device where the form cannot open.
- Test the form after 15 minutes and after disabling the feature: no decision should be sent.
- Distinguish provider acceptance from delivered/read status; inspect actual delivery receipts.

## Reference contracts

Meta-owned example (navigate Flow payload and nfm_reply):
https://github.com/WhatsApp/WhatsApp-Flows-Tools/blob/main/articles/creating-surveys/main.py
Meta Get Flow fields/status:
https://www.postman.com/meta/whatsapp-business-platform/request/9cwjfve/get-flow
Flow JSON reference:
https://developers.facebook.com/docs/whatsapp/flows/reference/flowjson

Live account access, publication, JSON validation and device checks remain pending.
Other ERP adapters, quiet hours, digests and MY REQUESTS are outside this patch.
