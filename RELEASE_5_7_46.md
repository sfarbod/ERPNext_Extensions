# erpnext_extensions 5.7.46

## A. Asset Scrap — Sub-Precision VAD Residual (Asset 2061)

### Summary

Version **5.7.46** fixes Asset Scrap failing with
`Row N: Both Debit and Credit values cannot be zero` when Iran whole-IRR ADS
normalization leaves a sub-currency Value After Depreciation (VAD) residual
after disposal-day depreciation (Asset `2061`: residual ≈ **0.44 IRR**).

No ERPNext core patch. Standard Debit/Credit validation remains enabled.

### Fix

After `depreciate_asset` and **before** the scrap Journal Entry:

1. If `|VAD| < currency threshold` (IRR → **0.5**), clear Asset + matching
   Finance Book VAD to **0**.
2. Meaningful amounts are never discarded.
3. Avoids a disposal P&L row that rounds to zero Debit/Credit.

---

## B. Asset Scrap / Disposal — Adjusted Gross after AVA

### Summary

The same release also corrects disposal accounting for assets that have
submitted Asset Value Adjustment (AVA) documents. Core ERPNext scrap/sale GL
uses `net_purchase_amount` only, so revaluation increments posted to Fixed
Asset / Revaluation Surplus were left stranded after scrap.

### Formula

```
adjusted_gross =
    net_purchase_amount
  + sum(submitted AVA difference_amount)   # signed; draft/cancelled excluded
  + additional_asset_cost                  # capitalized repairs on Asset
```

Asset **2061**: 2,500,000 + 3,738,720 + 0 = **6,238,720**.

### Scrap JE (exactly two rows after disposal-day depreciation)

| Account | Debit | Credit |
| --- | ---: | ---: |
| Fixed Asset | | adjusted gross |
| Accumulated Depreciation | adjusted gross | |

Revaluation surplus (`311303`) and Retained Earnings (`311301`) are **not**
posted in the Scrap JE. AVA surplus history remains on the original AVA
Journal Entries. Disposal P&L is only used when a genuine gain/loss remains
after depreciation (not for surplus transfer).

Sub-precision residual clear (section A) remains in force.

Sale / regain paths receive the same adjusted gross via a module patch on
`get_asset_disposal_values` / `get_gl_entries_on_asset_disposal` /
`get_gl_entries_on_asset_regain`.

### Files

- `erpnext_extensions/asset_usage_depreciation/services/disposal_revalued_gross.py`
- `erpnext_extensions/asset_usage_depreciation/disposal_patch.py`
- `erpnext_extensions/asset_usage_depreciation/services/scrap_restore_vad.py`
- `erpnext_extensions/asset_usage_depreciation/integration_hooks.py`
- `erpnext_extensions/hooks.py` (before_request / before_job patch)
- `erpnext_extensions/asset_usage_depreciation/tests/test_scrap_sub_precision_vad_v5746.py`
- `erpnext_extensions/asset_usage_depreciation/tests/test_disposal_revalued_gross_v5746.py`
- Related scrap/restore test updates

### Out of scope

- No Production changes; no version bump beyond **5.7.46**
- FX Sales Invoice Pattern A remains under **5.7.45**
