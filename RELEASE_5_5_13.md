# erpnext_extensions 5.5.13

## Canonical Manufacture — Department + issued-rate preservation

### Summary

Job Card Stock Rebuild canonical Manufacture rebuild now carries repair-owned metadata through submit:

1. **Department** — plan snapshot + SED copy from historical Manufacture / JC MTfM evidence (`stamp_canonical_departments`). Ambiguous or missing Department **blocks**. Does **not** depend on Custom 8.
2. **Rate preservation** — plan `issue_transfer` / `historical_mfg` rates survive Core `calculate_rate_and_amount` via the same `_bind_preserve_rates` / `_reapply_snapshot_rates` pattern as logistics recreate.
3. **Repair-scoped negative-stock guards** — backdated cancel/rebuild intermediate batch negatives are allowed only while `HISTORICAL_REPAIR_FLAG` is set (does not change Stock Settings).

### Canary PO-JOB09259

| Check | Result |
|-------|--------|
| Dry Run | PASS (`mutated=false`, `committed=false`) |
| Apply | PASS / COMMITTED |
| Canonical | MAT-STE-2026-40690 — 28/28 SED department = واحد بسته بندی - E |
| issue_transfer rates | 14640 / 146083 / 54629 / 54629 / 172000 preserved |
| Added economics | 1,385,019,456 IRR |
| Golden Rule | all OK / Remaining 0 |
| GL | balanced; Additional Costs = 0 |

### Non-goals / unchanged

- Golden Rule / MI / Component Scrap / Audit / Workstation isolation
- Weighted MTfM rate policy (still **latest**)
- `MANUFACTURE_COSTING_CONTRACT_VERSION = 5.3.43`
- Site validators (Department mandatory, Zero Rate script) remain enabled

### Tests

- `test_canonical_dept_rate_v5513` DPR / RATE / ACC / REG
- Playwright `playwright_job_card_dept_rate_v5513.mjs` (light)
