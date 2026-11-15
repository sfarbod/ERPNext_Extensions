# erpnext_extensions 5.7.40

## Asset Value Adjustment — Non-Depreciating Assets & Post-Revaluation Life-Final Balancing

### Summary

Version **5.7.40** fixes two Asset Value Adjustment (AVA) blockers found during
the production revaluation campaign:

1. **Non-depreciating assets** (`calculate_depreciation = 0`) could not complete
   AVA because core ERPNext always rebuilds the Asset Depreciation Schedule.
2. **Depreciating asset `3997`** failed AVA with a negative life-final balancing
   installment because Iran ADS normalize used `net_purchase_amount` instead of
   the post-revaluation carrying value.

Both fixes ship in **5.7.40**. No ERPNext core changes. No production AVA
documents are created by this release.

---

## A. Non-depreciating assets

### Problem

AVA `update_asset` always called `reschedule_depreciation` → ADS submit →
`validate_asset` throws when `calculate_depreciation = 0`. Production land-style
assets often retain orphan Finance Book rows, so the failure is reliable.

### Fix

`override_doctype_class["Asset Value Adjustment"]` skips ADS rebuild when
depreciation is disabled; JE + carrying-value update still run. Depreciation is
never flipped from 0 → 1, and no artificial schedule is created.

| Asset | Notes |
| --- | --- |
| `ن/89` | `calculate_depreciation = 0` |
| `ن/444` | `calculate_depreciation = 0` |
| `ن/447` | `calculate_depreciation = 0` |

### Files

- `erpnext_extensions/asset_usage_depreciation/asset_value_adjustment_override.py`
- `erpnext_extensions/hooks.py`
- `erpnext_extensions/asset_usage_depreciation/tests/test_asset_value_adjustment_non_depreciating_v5740.py`

---

## B. Life-final balancing after revaluation (asset 3997)

### Investigation — acquisition date = revaluation date?

| Field | Asset 3997 value | Equal to reval `2026-07-23`? |
| --- | --- | --- |
| Purchase Date | `2026-07-23` | **Yes** |
| Available For Use Date | `2026-07-23` | **Yes** |
| Depreciation Start Date | `2026-09-22` | No |

**Verdict: incidental, not causal.** The failure is an economics mismatch:
ERPNext rebuilds ADS from **`VAD − salvage`**, while
`authoritative_depreciable_total` used **`net_purchase − opening − salvage`**.
After upward revaluation (with JE links cleared), rounded rows 1..N−1 exceeded
the purchase-based total → negative life-final installment
(`−279,470,186,991` reproduced on development with rollback).

### Fix

| Item | Detail |
| --- | --- |
| `authoritative_depreciable_total` | `to_depr_amount(fb.value_after_depreciation − salvage)` |
| `normalize_ads_document` | Posted rows → `normalize_unposted_tail` on remaining `VAD − salvage`; else full-schedule normalize with corrected total |

At inception (`VAD == net_purchase − opening`) both formulas remain equal.

### Before / after (3997-shaped)

| Item | Before | After |
| --- | ---: | ---: |
| Authoritative total | 4,250,664,632,505 (`net_purchase`) | 4,545,567,146,822 (`VAD`) |
| Reschedule after upward reval | Throws negative life-final | Succeeds; sum = new VAD; no negative rows |

### Files

- `erpnext_extensions/asset_usage_depreciation/services/ads_amount_normalize.py`
- `erpnext_extensions/asset_usage_depreciation/tests/test_ava_life_final_reval_v5740.py`

---

## Version metadata

- `erpnext_extensions/__init__.py` → **`5.7.40`**
- `RELEASE_5_7_40.md` (this file)

## Tests

```bash
bench --site development.localhost clear-cache
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.asset_usage_depreciation.tests.test_asset_value_adjustment_non_depreciating_v5740 \
  --skip-before-tests
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.asset_usage_depreciation.tests.test_ava_life_final_reval_v5740 \
  --skip-before-tests
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.asset_usage_depreciation.tests.test_ads_amount_normalize \
  --skip-before-tests
```

## Deployment

1. Deploy `erpnext_extensions` **5.7.40**.
2. `bench --site <site> clear-cache` (restart workers if class overrides are cached).
3. Confirm AVA controller module ends with `asset_value_adjustment_override`.
4. Production revaluation of remaining assets remains a **separate authorized step**.
