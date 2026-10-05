# erpnext_extensions 5.5.6

## Job Card Stock Rebuild — Cancel Workflow State + Consumed* UI Clarity

### Summary

1. **Stale `workflow_state` after repair cancel — fixed (prevention only)** — Job Card Stock Rebuild cancellation of Stock Entries now stamps the configured Workflow cancellation state so successful cancel finishes as `docstatus=2` with a consistent cancel `workflow_state` (not leftover `Submitted`).

2. **Root cause** — Core `Document._save` skips `_validate()` when `_action == "cancel"`, so `set_workflow_state_on_action` never runs on a direct `doc.cancel()`. Interactive cancels go through `apply_workflow`, which stamps `next_state` first. Repair now mirrors that stamp without requiring a browser workflow action.

3. **No hard-coded `"Cancelled"`** — Cancel state is resolved from the active Workflow (prefer transition from current state → `doc_status=2`, else first `doc_status=2` state — same fallback order as Core `set_workflow_state_on_action`).

4. **Standard `doc.cancel()` preserved** — SLE/GL/`on_cancel` remain authoritative. Failed cancel does not stamp workflow state. Dry Run / Apply rollback restores both `docstatus` and `workflow_state`.

5. **Historical stale documents — report only** — Read-only diagnostic `scan_stale_cancelled_workflow` / `workflow_cancel.scan_stale_cancelled_stock_entry_workflow`. No mass cleanup in this release.

6. **UI: Consumed* clarified** — Label `مصرف از پای‌کار*` with help that source consumption is total physical Manufacture WIP outflow (Component Scrap already included; scrap is not deducted again). Backend field remains `proposed_consumed`.

7. **Accounting unchanged** — Golden Rule / scrap pairing / Manufacture Plan / temp Material Receipt bridge / costing / queue architecture untouched. Contract remains **5.3.43**.

### Scope

| Area | Change |
|------|--------|
| `_cancel_se` | Stamp + persist configured cancel workflow state |
| `workflow_cancel.py` | Resolver + read-only stale scan |
| Desk page | Consumed*/Scrap* label/help |
| Historical stale SE | Report only |

### Tests

- WC01–WC11 (`test_workflow_cancel_v556`)
- Playwright PW-WF01–PW-WF15 (`playwright_job_card_workflow_cancel_v556.mjs`)
- Related / bridge / audit / atomic regressions green on Development post-Apply canary

### Non-goals

- Do not mass-fix historical `docstatus=2` + `workflow_state=Submitted` rows
- Do not change Golden Rule remainder / Component Scrap pairing
- Do not change temporary bridge DocType/Purpose
