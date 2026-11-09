# Release 5.7.19 — Mandatory integer valuation_rate (replaces v5.7.18 ULP skip)

**Version:** 5.7.19  
**Branch:** `develop`  
**Runtime scope:** Iran Accounting purchase valuation + IRR residual classification  
**Deployment status:** Local Development. **No Production deploy until RIV rebuild verified.**

## Policy change

**Valuation Rate must always be an integer under IRR.** Fractional rates are not
permitted — including historical UVR `auth/qty` floats and ±1e-9 persistence drift.

### What v5.7.18 did (reverted)

`is_legacy_uvr_float_valuation_rate` accepted ±1 DECIMAL(30,9) quantum / IEEE ULP
drift, and `classify_amount_rate_residual` **skipped** economically reconstructing
fractional VRs (`amount_preserving_fractional_vr_zero_residual`). That kept rates
like `1,737,714.673913044` in the ledger path.

### What v5.7.19 does

1. **Write path** (`_align_purchase_valuation_rate_amount_preserving`): always
   `integer_valuation_rate_from_amount(auth, qty)` for FX, domestic, LCV, and
   PI `amount_difference` rows. No fractional branch.
2. **Classify path**: fractional VR with `|auth − qty×VR| ≤ 0.51` (or exact
   legacy UVR reconstruction) → **Class A** coerce to integer VR; residual
   `auth − qty×integer_VR` posts via existing Stock Adjustment / Round Off path.
3. **ULP tolerance removed** from the legacy UVR detector.
4. True non-reconstructing fractional rates remain **Class B** fail-closed.

Example (`MAT-PRE-2026-02045` / `15010547` qty=46, auth=79,934,875):

| | Before | After |
|--|--|--|
| VR | 1,737,714.673913044 | **1,737,715** |
| Inventory | 79,934,875 | 79,934,890 |
| Residual | (skipped) | **−15** Class A SA |

## Asset Purchase Receipt GL

Asset PRs have **no SLE**. Rebuild original PR GL via RIV
`based_on=Transaction` + `repost_only_accounting_ledgers` after correcting
`expense_account` on the PR item — **not** via manual Journal Entries.

## Tests

- `test_integer_vr_enforcement_v5719.py` — PI-discount / LCV integerize; Class A; Class B mismatch
- Updated `test_pr_lcv_amount_preserving_vr_v5625.py`, `test_pr_legacy_uvr_float_v5528.py`
- `test_pr_uvr_ulp_drift_v5718.py` aliases the v5.7.19 suite
