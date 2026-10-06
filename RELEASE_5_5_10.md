# erpnext_extensions 5.5.10

## Job Card Golden Rule Audit — Job Card × Item (batch ignored)

### Summary

1. **Report grain** — `Job Card Golden Rule Audit` now reconciles at **Job Card × Component Item**. Batch is ignored for report totals and status.
2. **Status** — Primary statuses simplified to **BALANCED** / **REVIEW**, with a concise **Reason** field (never `BATCH_MISMATCH`).
3. **Filters** — Batch column and Batch filter removed. Default **Show Balanced = OFF** (operators see REVIEW only).
4. **Summary** — Compact header: Total / Balanced / Review Job Cards (+ item row counts). Job Card rollup is BALANCED only when all item rows are BALANCED.
5. **Scope** — REPORT ONLY. Reuses `scan_golden_rule` evidence; does not mutate stock/accounting and does not change Stock Rebuild / Manufacture / Component Scrap / contract logic.

### Non-goals / unchanged

- Job Card Stock Rebuild repair engine (batch-sensitive)
- `manufacture_plan.py` / `atomic_repair.py` repair semantics
- Component Scrap pairing & pricing
- `MANUFACTURE_COSTING_CONTRACT_VERSION = 5.3.43`
- Golden Rule physical-consumption model (scrap not double-counted)

### Canary PO-JOB08760 / 13200544

| Field | Value |
|-------|-------|
| Issued | 1160 |
| Returned | 12 |
| MFG Consume | 1148 |
| Remaining | 0 |
| Status | BALANCED |
| Grain | 1 row (no batch split) |

### Tests

- `test_golden_rule_audit_v5510` — GA01–GA12 (+ read-only mutation check)
- Playwright lightweight — `playwright_job_card_golden_rule_audit_v5510.mjs` PW-GA01–PW-GA06
