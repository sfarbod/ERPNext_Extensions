# erpnext_extensions 5.6.8

## CASE E — Manufacture SED overclaim / VALUATION_CONTINUITY (30100298)

### Symptom
Historical Rebuild Campaign fail-fast on Item `30100298` /
warehouse `انبار پایکار خط تولید اسپاد فارمد` with:

- SLE `MAT-SLE-2026-294684` / voucher `MAT-STE-2026-36178-1`
- `VALUATION_CONTINUITY` expected ≈ −35.9T, actual `0`
- classification `CONFIRMED_CORRUPTION`

### Root cause
1. Manufacture SED stamped a poisoned `outgoing_rate` (~1.49e12).
2. Core MA wrote `stock_value_difference = −rate×qty` while previous
   warehouse `stock_value` was already near empty, then forced
   `stock_value = 0` at qty_after=0 → discontinuous SLE.
3. Iran ghost classification used `value_before = sv − svd`, which
   invents a fictional previous equal to `−svd` (ghost≈0 / NOOP) and
   left the discontinuity in place.

### Fix (permanent)
- Resolve consume-to-zero previous layer from the chronological previous
  SLE (same-voucher prior rows included), not from `sv−svd` fiction.
- Cap Manufacture SED overclaim to the available layer when the layer is
  within `MANUFACTURE_AUTO_SA_MAX` (does **not** auto-expense trillions).
- Clear Manufacture sticky overclaim `outgoing_rate` before Core MA during RIV.
- Final `_repair_empty_warehouse_valuation_continuity` safety net after sync /
  vanilla zero-qty restore.
- I3/I4 and `TRANSITIONAL_NON_BLOCKING` preserved.

### Tests
`test_case_e_30100298_valuation_continuity_v568` (+ CASE A/D still green).

### Production note
Production HRC-00002 failed earlier on item `13100018` with a truncated
MariaDB traceback — that SQL class is **not** claimed fixed here. Operator
must capture the full exception (errno / SQLSTATE) before Production restart.
