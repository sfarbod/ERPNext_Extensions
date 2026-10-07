# erpnext_extensions 5.5.18

## Iran Accounting — preserve Co-Product stage allocation during residual align

### Summary

1. **Bug** — For Manufacture with stage-participating `CO_PRODUCT` and header
   operating cost, `allocate_stage_output_cost` correctly closed material and
   operating-cost pools (FG remainder). Immediately afterward,
   `align_manufacture_finished_good_residual` recomputed FG `basic_amount` from
   `outgoing − Σ non-FG amount`. Because Co-Product `amount` embeds
   `additional_cost`, the aligner subtracted Co-Product OH from FG **material**,
   inventing Stock Adjustment equal to that OH share.

2. **Fix (Option A)** — When `has_stage_participating_co_product(doc)` is true
   (v5.3.3+ contract, finance-excluded independent By-Products excluded), skip
   the legacy single-FG pool-close. Integer valuation-rate polish and header
   total refresh still run. Mirrors the conceptual Product Reject guard from
   5.5.7 without treating every By-Product as stage-owned.

3. **Regression** — `test_co_product_residual_align_v5518` covers the mini
   pool (1000/100, 9+1), full `apply_irr_manufacture_economic_finalize`,
   finance-excluded By-Product, multiple Co-Products, fractional factors,
   zero OH, simple MAIN_FG, and component scrap. Existing Product Reject /
   TYPE C / stage suites remain green.

### Scope

| Area | Change |
|------|--------|
| `manufacture_stage_costing.py` | `stage_participating_co_product_rows`, `has_stage_participating_co_product` |
| `manufacture_rounding.py` | Early return when stage owns participating CO_PRODUCT |
| Tests | `test_co_product_residual_align_v5518.py` |

### Non-goals

- No historical Stock Entry / SLE / GL repair in this release.
- No change to Co-Product OH allocation itself (BP `additional_cost` remains
  legitimate stage share).

### Tests

- Focused: `test_co_product_residual_align_v5518`
- Related: Product Reject residual align, manufacture IRR residual, stage
  equivalent costing, stage by-product bridge, manufacture residual submit
