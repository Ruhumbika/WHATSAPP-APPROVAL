# WhatsApp conversation improvements — Phase 5

Apply this patch after Phase 4 menu/admin. Gateway only; PETA and frontend do not change.
All WhatsApp copy is English. No new Meta template names or environment settings are required.

## Changed behavior

- HEY/HI/HELLO detects unfinished rejections. Choose Continue rejection, View request, Cancel rejection, Main menu or User guide.
- Continue rejection sends a fresh prompt in the current chat: rejection-type buttons, reason entry, or the saved review with Submit/Edit/Cancel buttons.
- Multiple unfinished rejections are listed with pagination. Only the selected request is resumed.
- Resume checks the current actor, workflow snapshot, task expiry and conversation expiry.
- Request context includes reference, purpose, selected rejection type and amount when available from the verified snapshot; otherwise it directs the user to the PDF.
- View request uses the authorized PDF path without approval/decision buttons and does not submit or delete the draft.
- MENU pauses unfinished rejections. Main menu includes Resume rejection.
- Reason text saves a draft; it NEVER immediately submits a rejection.
- Submit rejection explicitly confirms the displayed reason and rejection type.
- Edit reason invalidates the old confirmation token. The previous draft remains saved until replacement.
- Cancel rejection clears the draft and closes only that conversation. The approval request remains pending.
- Draft/session expiry and changed ERP assignment prevent confirmation.
- Duplicate submits do not create another decision/callback.
- Help, menus and PDF viewing never submit a decision.
- Admin controls appear directly in the main menu for environment-authorized numbers.
- User guide includes Resume & drafts; admins additionally see Admin controls documentation.
- Invalid/expired actions offer Pending approvals, Resume rejection and Main menu instead of leaving users with an API error.
- Submission acknowledgement explicitly says Waiting for PETA confirmation.
- Final success is sent only after the existing ERP applied result. Exhausted callback delivery reports inability to confirm, not an assumption that the ERP did or did not apply the action.
- Failure notices do not consume the final-result slot; later verified success can still be reported.

A long reason is sent in full as text followed by the review buttons. Short reviews fit in one interactive message.
Resume routes to the existing notification queue, avoiding a duplicate acknowledgement. The next worker cycle sends the current prompt.
Existing unfinished Phase 4 conversations can be resumed. All reasons entered after this upgrade require explicit confirmation, including conversations started before deployment.
Drafts remain usable only while the existing rejection session and approval task are valid; MENU does not extend their expiry.
For safety, an explicit reply to a particular task's prompt can still address that task while normal unthreaded messages stay in the menu.
Queue status may show "routed" for a menu action handed to the rejection notification queue; this is not delivery confirmation.
MY REQUESTS, quiet hours, digest and full per-message delivery monitoring remain future work.

## Laptop installation

From the gateway repository:

    git apply --check ~/Downloads/whatsapp-conversation-phase5/gateway-conversation-phase5.patch &&
    git apply ~/Downloads/whatsapp-conversation-phase5/gateway-conversation-phase5.patch &&
    python3 -m unittest discover -s tests -q &&
    git diff --check

Expected: 192 passing tests. Negative-case fixture logs for 403, 503 and notification context are expected with OK.
Stop if --check fails. Provide the exact error and git status --short; do not force apply.
No .env, editor configuration or local backup files are included.

## Deployment after laptop tests

Commit the files included in this patch, push through the normal repository workflow, and pull on the gateway VPS.
The existing startup migration adds three nullable/defaulted draft/pause fields to approval_requests.
The old rejection_phase constraint is preserved. No destructive schema rewrite or PETA migration is required.
Restart both services together so the webhook server and worker use the same confirmation logic:

    cd /opt/whatsap-approval &&
    git pull --ff-only &&
    sudo systemctl restart approval-gateway approval-gateway-worker &&
    sudo systemctl is-active approval-gateway approval-gateway-worker

Keep WHATSAPP_ADMIN_PHONES configured as in Phase 4.

Do not roll back to Phase 4 while users are testing drafts: Phase 4 submits a reason immediately.
If rollback is necessary, stop both services first and coordinate the changed user flow.

## Live acceptance

Use a dedicated test request and authorized reviewer.

1. Start Reject but stop before choosing a type. Send HEY -> Continue rejection.
   Fresh Return to edit / Reject completely / Cancel buttons must arrive in this chat.
2. Choose a type. Send HELP. No decision/callback should be submitted.
3. Enter a reason. Confirm request stays pending and review buttons appear.
4. Select Edit reason, replace it, then test the previous Submit button.
   The stale button must not submit either reason; use the current review.
5. Send MENU. Navigate User guide or, as admin, Admin controls.
   Resume rejection must restore the saved review.
6. View request. Confirm PDF arrives and no decision is submitted.
7. Submit the current review on the test request. Repeat the click.
   Exactly one decision is recorded. Waiting-for-PETA and confirmed outcomes must be distinct.
8. On another test request, Cancel rejection and confirm the approval remains pending.
9. From a nonadmin, confirm main menu and guide do not expose admin controls.
10. Test multiple unfinished requests and an expired/stale action.

Automated verification covers draft/confirm/edit/cancel, duplicate and stale clicks, changed workflow, expiry,
read-only PDF, paused navigation, admin discovery, review button parsing and later confirmation after delivery failure.
Meta/ERP transport is mocked in automated tests. Real rendering and live delivery still require the checklist above.
