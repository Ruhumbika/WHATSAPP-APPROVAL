# Workflow messages: first implementation stage

This patch builds on the Directory phone synchronization deployed as 7c4acf5.
Apply it to the gateway on the laptop, run tests, then deploy through GitHub.
No Laravel source change is required for this stage. The current PETA progress
endpoint continues to authorize access for both requisitions and payment advices.

## Included

- `STATUS REQ 298` and `STATUS PA 123` queue the approved English
  `workflow_progress_update` template (seven positional body parameters).
- Its `View progress` quick reply fetches the current full journey from PETA.
  It is read-only and does not approve or reject anything.
- Both inbound handling and queued delivery verify Directory phone/mapping and
  PETA record access. Duplicate inbound message IDs are not resent. Unknown
  provider acceptance is not retried automatically.
- Rejection choices use three interactive buttons: Return to edit,
  Reject completely, Cancel. Numeric replies 1/2/3 still work.
- The AGR identifier stays in the button payload; it is no longer visible
  in rejection prompts. A reason is still mandatory. Sending the reason
  submits the decision, as stated in the prompt; there is no extra confirmation.
- Requisition item previews show up to two names/quantities, with a count of
  additional entries. Full rates and items remain in the attached PDF.

The existing `WHATSAPP_TEMPLATE_LANGUAGE` must match the approved language
of `workflow_progress_update` (English). The approval template name does not change.
Static headings and sections of the existing approval template are unchanged.

## Local verification

```bash
git apply --check ~/Downloads/gateway-workflow-messages-phase1-v2.patch &&
git apply ~/Downloads/gateway-workflow-messages-phase1-v2.patch
python3 -m unittest discover -s tests -q
git diff --check
```

Startup migration adds one nullable `payload_json` column to the existing
`decision_repeat_notifications` queue. It preserves existing rows and is
repeatable. Do not clear queues or requeue old approvals to test the layout.

After deployment, an authorized reviewer can send `STATUS REQ 298`, then tap
View progress. Test rejection using a designated test requisition: Reject,
Return to edit, then CANCEL. Verify no ERP decision is applied on cancellation.

## Remaining stages

This stage does not yet send automatic progress updates to request creators,
schedule `workflow_action_reminder`, or deliver `workflow_completed_document`.
Those require recipient selection, workflow-aware scheduling/deduplication,
and confirmed final-completion document delivery. Template approval alone does
not activate these behaviors.
