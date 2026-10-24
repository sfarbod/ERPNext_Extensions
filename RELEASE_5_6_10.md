# erpnext_extensions 5.6.10

## Physical Two-Slice integer IRR batch valuation (Delivery Note / Sales Invoice)

### Symptom
Outgoing Delivery Notes with batch Moving Average pools that are not divisible into a
single integer rate (example: batch `2984-20100102-ak253006a51`, qty 10 from pool
`151 / 544,601,298`) were blocked by the integer SABE guard. Core wrote a fractional
MA rate; Iran Accounting correctly refused to persist it, but had no outgoing
integerization path for Delivery Notes (TVA/Stock Adjustment remains Stock Entry only).

### Policy (approved)
- `out_value = ROUND_HALF_UP(pool_value × out_qty / pool_qty)`; full deplete → pool value
- Represent as Physical Two-Slice SABE rows at integer rates `R` and `R+1`
- Every slice satisfies `abs(SVD) = ROUND_HALF_UP(abs(qty) × integer_rate)`
- No Stock Adjustment workaround; historical fractional pools fail closed

### Fix (Approach A)
1. After Core `update_serial_batch_no_valuation`, rewrite IRR outgoing DN/SI SABE rows
   via `allocate_physical_two_slice` + `apply_plan_to_bundle`.
2. Scoped duplicate-batch exception only for authorized Physical Two-Slice shapes on
   DN/SI outward bundles (arbitrary duplicates still rejected).
3. Rewrite also runs during Repost Item Valuation so Core MA cannot reintroduce
   fractional SABE rates on DN/SI.
4. Bundle `avg_rate` remains a derived aggregate (`total_amount / total_qty`); SABE
   row rates/amounts are the valuation source of truth.

### Evidence (`development.localhost`)
- Submitted `MAT-DN-2026-00014` detail `dhi8m89p87`:
  - SABE: `−9 × 3,606,631 = −32,459,679` and `−1 × 3,606,632 = −3,606,632`
  - SLE `99b90a5d4d`: qty `−10`, SVD `−36,066,311`
  - No Stock Adjustment
- Repost Item Valuation reconstructed identical slices (idempotent on repeat)
- Same-batch multi-row sale/zero-rate rows on the primary DN keep separate commercial
  lines and integer valuation
- Synthetic multi-row + cancel restored batch pool exactly
- E2E harness: `e2e_v5610_physical_two_slice.run_all` → 14/14

### Files
- `iran_accounting/domain/physical_two_slice.py`
- `iran_accounting/domain/physical_two_slice_bundle.py`
- `iran_accounting/integration/monkey_patches.py` (guard + duplicate validate wiring)
- `iran_accounting/e2e_v5610_physical_two_slice.py`
- `iran_accounting/tests/test_physical_two_slice.py`

### Out of scope
- Automatic repair of historical fractional pools (blocked with diagnostic)
- ERPNext/Frappe Core source changes
- Approach B (dual SLE per DN Item)
