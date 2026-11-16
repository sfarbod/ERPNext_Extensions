# erpnext_extensions 5.7.46

## Asset Scrap — Sub-Precision VAD Residual (Asset 2061)

### Summary

Version **5.7.46** fixes Asset Scrap failing with
`Row N: Both Debit and Credit values cannot be zero` when Iran whole-IRR ADS
normalization leaves a sub-currency Value After Depreciation (VAD) residual
after disposal-day depreciation (Asset `2061`: residual ≈ **0.44 IRR**).

No ERPNext core patch. Standard Debit/Credit validation remains enabled.
Asset Disposal / Sale / Value Adjustment paths are unchanged.

---

### Root cause

1. Disposal-day depreciation (via Iran ADS normalize) posts a **whole-rial**
   write-off against the remaining book value.
2. A fractional residual can remain on Asset + Finance Book VAD (e.g. **0.44**
   after AVA / float history).
3. Core `create_journal_entry_for_scrap` → `get_gl_entries_on_asset_disposal`
   builds a disposal P&L row for that residual.
4. Journal Entry currency rounding (IRR precision **0**) collapses the row to
   zero Debit and Credit → validation error.

**Row 3 (failure shape):** Gain/Loss on Asset Disposal, Debit **0.44** → rounds
to **0**, Credit **0**.

---

### Fix

After `depreciate_asset` and **before** `create_journal_entry_for_scrap`:

1. If `|VAD| < currency threshold` (IRR → **0.5**), clear Asset + matching
   Finance Book VAD to **0**.
2. Amounts at or above the threshold are left untouched.
3. Scrap JE then uses the standard fully written-off two-line shape
   (Cr Fixed Asset / Dr Accumulated Depreciation) with no zero P&L row.

Restore path: after cancelling scrap-day depreciation, refresh in-memory Asset
and Finance Book VAD from the database so ADS rebuild uses the restored
carrying value.

Order of operations:

```
validate → depreciate_asset → clear_sub_precision_scrap_vad_residual
         → create_journal_entry_for_scrap → VAD sync → usage replan
```

### Accounting behavior

| Scenario | Behavior |
| --- | --- |
| Sub-rial residual after disposal depr | Cleared; scrap JE is two-line, balanced |
| Meaningful remaining book value | Unchanged; disposal P&L row kept |
| Zero book value | Two-line scrap JE (Cr FA / Dr Accum) |
| Restore after scrap | Cancel-first scrap-day unwind; VAD restored to whole-rial carrying value |

### Files

- `erpnext_extensions/asset_usage_depreciation/services/scrap_restore_vad.py`
- `erpnext_extensions/asset_usage_depreciation/integration_hooks.py`
- `erpnext_extensions/asset_usage_depreciation/tests/test_scrap_sub_precision_vad_v5746.py`
- Related updates: `test_scrap_restore_rpc_boundary.py`,
  `test_scrap_restore_hardening_v5553.py`, `test_scrap_restore_vad_v5553.py`

### Regression tests (Development)

| Suite | Result |
| --- | --- |
| `test_scrap_sub_precision_vad_v5746` | 9/9 (incl. Asset 2061 scrap→restore) |
| `test_scrap_restore_rpc_boundary` | 5/5 |
| `test_scrap_restore_hardening_v5553` | 9/9 |
| `test_scrap_restore_vad_v5553` | 9/9 |

### Anchor verification (Asset 2061)

| Check | Result |
| --- | --- |
| Scrap after residual clear | Success |
| Scrap JE shape | 2 rows, balanced (Cr FA / Dr Accum = net purchase) |
| Zero Debit/Credit rows | None |
| Restore | Asset returns to Submitted; scrap JE cancelled |

### Out of scope

- No Production scrap / restore / data changes
- No version push or deploy in this change set
- FX Sales Invoice Pattern A remains documented under **5.7.45**
