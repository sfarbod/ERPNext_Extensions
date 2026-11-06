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

1. After SABE SLE alignment, re-apply Case D via `_apply_case_d_after_sabe_align`.
2. After document-amount authority reassert (Manufacture/Repack path), re-apply the
   same Case D guard.
3. Monkey-patch no-bundle / hot-upgrade `process_sle` path calls the same helper
   after SABE + reassert.
4. Idempotent: no-op when `stock_value` is already non-negative with remaining qty.

Does **not** bypass the ledger contract validator. Does **not** create artificial
SA / JE / Round Off. Preserves integer IRR and `ROUND_HALF_UP`. Preserves SED
amount authority (v5.7.3) and does not double-count additional_cost / LCV.

### Explicitly unchanged / limitations

- Sequential resume/reconcile architecture (v5.7.2) unchanged.
- Fail-fast on true `CONFIRMED_CORRUPTION` when Case D cannot apply.
- Do **not** auto-resume `HRC-00009`. Prefer a **new** Production campaign for
  remaining work.
- Case D may leave positive remaining qty with `stock_value = 0` when historical
  SED/SABE rates overshoot the capitalized layer. That is not proof of a correct
  historical unit rate; it only prevents `NEGATIVE_STOCK_VALUE`.

### Development validation (bounded)

Scoped campaign `HRC-00010` (Dev only) reprocessed the single failed identity with
v5.7.4: RIV Completed; target SLE `MAT-SLE-2026-210109` went from
`sv=-155330802` to `sv=0`; identity-wide `qty>0 && sv<0` count went 197 → 0.

### Tests

New: `test_sabe_case_d_after_align_v574.py`

Verified regression suites (Bench / `development.localhost`):

| Suite | Result |
|-------|--------|
| `test_sabe_case_d_after_align_v574` | 2/2 |
| `test_case_d_30100023_negative_sv_v561` | 4/4 |
| `test_sabe_integer_valuation_v570` | 11/11 |
| `test_sequential_pause_resume_v572` | 19/19 |
| `test_se_ledger_amount_authority_v573` | 8/8 |

### Version

`erpnext_extensions.__version__` → **5.7.4** (flit dynamic version from `__init__.py`)
