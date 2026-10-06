# erpnext_extensions 5.5.12

## Job Card Golden Rule Audit — Job Card Status + Operation filters

### Summary

Report-only enhancement to **Job Card Golden Rule Audit**:

1. **Job Card Status** column + filter — uses authoritative `Job Card.status` Select options from DocType meta (`Open`, `Work In Progress`, `Partially Transferred`, `Material Transferred`, `On Hold`, `Submitted`, `Cancelled`, `Completed`). `Submitted` is a real status value (not merely `docstatus=1`).
2. **Operation** column + filter — uses `Job Card.operation` (Link → Operation). Blank Operation displays blank; specific Operation filter excludes NULL/blank.
3. Filters applied in bulk `list_job_cards_in_range` so **summary counts** respect Status / Operation / intersection.
4. Grain remains **Job Card × Item** (batch ignored). Existing Show Balanced / BALANCED·REVIEW behaviour unchanged.

### Non-goals / unchanged

- Job Card Stock Rebuild / Workstation isolation (5.5.11)
- Golden Rule repair semantics
- `MANUFACTURE_COSTING_CONTRACT_VERSION = 5.3.43`
- Accounting / SLE / GL / Component Scrap

### Canary PO-JOB08760

| Field | Value |
|-------|-------|
| Job Card Status | Completed |
| Operation | 1.2 mL  بسته بندی |
| 13200544 | Issued 1160 / Returned 12 / Consumed 1148 / Remaining 0 / BALANCED |

### Tests

- `test_golden_rule_audit_filters_v5512` GF01–GF20
- Playwright `playwright_job_card_golden_rule_audit_v5512.mjs` PW-GF01–PW-GF12
