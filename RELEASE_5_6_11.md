# erpnext_extensions 5.6.11

## HRC-00004 — Historical fractional SABE pool blocks Physical Two-Slice

### Symptom (Production `HRC-00004` / session `20261024195815`)
Failed RIV `arpd4l8e70` for Item `13100012` @ packaging approved warehouse:

`AllocationError: historical fractional pool_value=…; repair required before allocate`

Call chain: Core RIV → `update_serial_batch_no_valuation` →
`rewrite_outgoing_bundle_after_core` → `allocate_physical_two_slice`.

### Root cause
1. **Historical / Core MA float on Serial and Batch Entry** for FG batches
   (example: Material Transfer `MAT-STE-2026-33527`, batch
   `7060-20100006-MR265001D01`, SABE rate `250653.505702364` while SED rate is
   integer `250654`). Pool aggregates become non-integer.
2. **RIV dependency expansion**: Core queues dependent Item/Warehouse pairs
   from vouchers that touch the seed identity. Packaging-material RIVs therefore
   revalue FG Delivery Notes that hit Physical Two-Slice.
3. **5.6.10 RIV skip**: For non-DN/SI vouchers during RIV the fractional SABE
   guard returned early, so Core MA **reintroduced** float rates on Stock Entry
   SABE mid-campaign, then DN two-slice fail-closed on the poisoned pool.

Failure 2 (`mnqsl58oke` / I5 on `MAT-STE-2026-25106`) did **not** reproduce on
the morning `20261024_085950` backup with current code (`13100060` Quarantine
RIV Completed). Treat as separately evidenced; may be state-dependent after
partial HRC mutation.

### Fix
1. **`historical_fractional_sabe_repair`**: explicit ROUND_HALF_UP integerization
   of SABE rate/SVD with audit JSON under
   `private/files/iran_fractional_sabe_repair/`.
2. During RIV, after Core MA on non-DN/SI bundles: integerize SABE and refresh
   in-memory `doc` totals (no longer skip silently).
3. Before two-slice allocate: if pool value is fractional, run batch@warehouse
   repair, re-read pool; still fail closed if residual fraction remains.

### Tests
- `test_historical_fractional_sabe_repair_v5611`
- Existing `test_physical_two_slice` + 1020 retry suites remain green
- Dev reproduction: sequential RIV for `13100012` packaging approved must Complete

### Migration
No schema migration. First RIV that touches a fractional batch writes audit
files on the site.

### Production recovery
Do **not** delete `HRC-00004`. Install 5.6.11, clear stop flag / executor lock,
resume sequential rebuild from existing session `identities_done` (preserve
completed RIVs). See operator runbook in the investigation report.
