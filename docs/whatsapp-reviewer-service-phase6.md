# Phase 6 — Reviewer synchronization and service-aware menus

Base: Phase 5 conversation patch (192 tests). Gateway-only patch; no PETA/frontend migration.
Expected after apply: 202 passing tests.

## Reviewer synchronization

ERP state remains authoritative. A WhatsApp click alone does not cancel other reviewers.

- After an applied callback, wake the workflow watch for immediate ERP reconciliation.
- Existing ERP workflow events retire assignments absent from the verified new assignment set.
- Poll watched workflows approximately every 60 seconds for active stages and 300 seconds otherwise,
  subject to worker queue capacity. This catches missed ERP events and actions made directly in PETA.
- Existing numeric PETA pending request references are enrolled during startup migration.
- If ERP still lists the same reviewer at the same step, preserve the task.
- Retiring a task closes its rejection conversation, clears draft/review token and expires queued prompts.
- Stale stage reminders are cancelled. Old actions offer recovery to the current inbox/menu.
- Queue one retirement notice per affected reviewer/request/state, then record that each retired task has been covered.
- Never notify for an undelivered old task unless it had an unfinished rejection.
- Notices reverify the Directory identity and current phone before sending.
- Replaced snapshots say the earlier task was replaced; they do not claim final workflow completion.

Retirement notices reuse the approved workflow_progress_update template and its existing seven body parameters.
For a draft, the status parameter says the unfinished rejection was closed without submission.
The template's progress button uses the existing ERP-authorized progress endpoint. If ERP no longer allows
that user to view the record, recovery to the menu remains available.

WORKFLOW_NOTIFICATIONS_ENABLED still controls proactive sends. Local reconciliation continues when it is off.
No new template name is introduced. Check real template rendering in live acceptance.

## Service identity

- Directory's verified service ID and service name determine the assistant heading.
- Multiple verified services offer Choose service / Switch service.
- Unknown/unverified users see ABOOD GROUP OF COMPANIES ERP ASSISTANT.
- Directory failures do not mean "user has no service"; the assistant reports temporary verification failure.
- Admin authority remains exclusively WHATSAPP_ADMIN_PHONES; service membership grants no approval rights.
- Request summaries identify their service and retain company context.
- An unsupported service does not accidentally route its request command to PETA.

Important boundary: discovery verifies existing active gateway identities against configured adapters.
The provided Directory contract accepts user UUID/ID plus service ID. It does not expose a verified
phone-to-all-services discovery endpoint. This patch cannot discover a completely unknown Directory user
by phone or enumerate services for which no adapter/identity is configured. It uses the generic heading.
PETA remains the implemented inbox backend. Other ERP inboxes need their own adapter; choosing a service
does not manufacture those endpoints or permissions.

## Less chat noise

- Selecting a pending-list row shows a short summary first.
- View document opens the existing authorized PDF/action flow.
- Next pending request opens the refreshed pending list.
- Existing direct REQ/PA commands retain their document behavior.
- Best-effort read receipt and typing indicator run before a new inbox/menu job, with a two-second timeout.
- Failure of that optional indicator never blocks the actual response.

This release does NOT publish WhatsApp Flows, create new proactive templates, add quiet hours/digests,
or change Meta Business profile/display names. Flows require separate account readiness and publication.

## Laptop apply

    git apply --check ~/Downloads/whatsapp-reviewer-service-phase6/gateway-reviewer-service-phase6.patch &&
    git apply ~/Downloads/whatsapp-reviewer-service-phase6/gateway-reviewer-service-phase6.patch &&
    python3 -m unittest discover -s tests -q &&
    git diff --check

If --check fails, stop and share the exact error and git status --short. Do not force apply.
Test fixtures deliberately log unavailable context/403/503 errors; judge success by the final OK.
The approval-only test fixture explicitly mocks the reconciliation endpoint; no external test host is needed.

## Deployment

After laptop tests, commit the patch's named source/test/doc files, push through the normal Git workflow,
then pull on the gateway VPS and restart both services together:

    cd /opt/whatsap-approval &&
    git pull --ff-only &&
    sudo systemctl restart approval-gateway approval-gateway-worker &&
    sudo systemctl is-active approval-gateway approval-gateway-worker

Startup performs additive SQLite changes. Existing admin environment settings remain valid.

## Live acceptance

1. Use one test requisition with two reviewers at the same stage.
2. Reviewer A starts a rejection and saves a draft without submitting.
3. Reviewer B approves; wait for the ERP applied result and reconciliation.
4. Where ERP says the stage is finished, A receives one update, the draft disappears from Resume rejection,
   reminders stop, and the old button cannot submit. Check the update template's status text.
5. Repeat with an ERP action made through the PETA application rather than WhatsApp.
6. For a workflow that STILL requires both reviewers, confirm A remains pending after B's action.
7. Send MENU from a known phone: verified service heading. From an unknown phone: group heading.
8. If multiple configured services exist, verify service choice and that an unsupported service cannot open PETA requests.
9. Select a pending request: summary first, then View document.
10. Confirm typing/read receipt where supported; provider failure must not suppress the menu.

Automated tests mock Directory, ERP and Meta. Real device rendering/delivery and service memberships require
these live checks. Do not treat sent/provider acceptance as confirmed delivery or read.
