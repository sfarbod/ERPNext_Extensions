# erpnext_extensions 5.7.4

## HRC-00009 Sequential fail-fast — Case D re-poison after SABE align

### Incident (latest Production backup `20261106_160923`)

Campaign `HRC-00009` status **Failed** (not the earlier Pause/`in_progress_unresolved`
shape). Session remark `SEQ_SESSION=20261106152255`.

| Field | Value |
|-------|-------|
| Failed RIV | `fc2nf25o2f` |
| Identity | `13100023` @ انبار approved اقلام بسته بندی اولیه اسپاد |
| Fail-fast | `NEGATIVE_STOCK_VALUE` / `CONFIRMED_CORRUPTION` |
| First poisoned SLE | `MAT-SLE-2026-210109` on `MAT-STE-2026-29941` |
| Arithmetic | available `632210600` − SABE `1497227×526` → `stock_value=-155330802`, qty `9274` |

Concurrent MariaDB deadlock (`1213`) at 16:02:03 was from an unrelated Stock Entry
`apply_workflow` submit (BatchNoValuation), **not** the sequential fail-fast cause.
Negative SLEs were written at **15:53:23** during RIV itself.

v5.7.2 Pause/Resume controls remain present; this failure is a **valuation** defect
that correctly tripwired fail-fast.

### Root cause

1. Historical Material Transfer for Manufacture SED/SABE rates (`1497227`) far exceed
   the capitalized warehouse layer (`~346357` incoming).
2. Iran Case D (`reconstruct_nonnegative_remaining_layer`) can repair qty>0 / sv<0
   during `sync_irr_sle_from_stock_entry_row`.
3. Later **SABE integer align** (`qty × ROUND_HALF_UP(rate)`) re-stamped the poisoned
   bundle amount onto the SLE and re-created `NEGATIVE_STOCK_VALUE`.
4. Sequential coverage verify fail-fasted; campaign set to Failed. No duplicate RIV
   create observed for this identity.

### Fix

After SABE SLE alignment (and after document-amount reassert), re-apply Case D via
`_apply_case_d_after_sabe_align` so remaining positive qty never keeps negative
`stock_value`. Idempotent. Does not bypass the ledger contract validator. Does not
create artificial SA/JE/Round Off.

### Explicitly unchanged

- Sequential resume/reconcile architecture (v5.7.2).
- Fail-fast on true CONFIRMED_CORRUPTION when Case D cannot apply.
- No auto-resume of HRC-00009.

### Tests

`test_sabe_case_d_after_align_v574.py`

### Version

`erpnext_extensions.__version__` → **5.7.4**
