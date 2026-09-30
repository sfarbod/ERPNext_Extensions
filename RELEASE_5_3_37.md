# Release 5.3.37 — Late Product Reject Pre-Core Bridge

## Summary

- Reused/generalized the pre-Core valuation bridge pattern introduced for stage-equivalent outputs.
- Added lifecycle support for late-added `MAIN_PRODUCT_REJECT` rows (e.g. Server Script Custom 14 after app `before_validate`).
- Product Reject economic valuation remains owned by `allocate_scrap_absorbed_cost`.
- Product Reject valuation derives from current source/WIP production economics, not destination warehouse history.
- No generic zero-valuation Scrap behavior was introduced.
- Existing Co-/By-Product allocator (`allocate_stage_output_cost`) remains unchanged.

Target canary: Job Card `PO-JOB10094` → Manufacture Draft → Submit/Cancel (rollback).

## Problem

Lifecycle ordering:

```text
Iran before_validate → permit_scrap (Scrap row absent)
→ Server Script Custom 14 appends Product Reject Scrap
→ Core StockEntry.validate → get_valuation_rate(destination scrap WH)
→ Valuation Rate Missing
→ Iran allocate_scrap_absorbed_cost never reached
```

## Fix

1. **Qualification** — fail-closed `is_product_reject_bridge_candidate`
   (Manufacture, IRR contract, incoming Scrap, same-as-FG Product Reject,
   consume rows present, not Component Scrap / stage secondary / explicit
   independent valuation).
2. **Pre-Core bridge** — `permit_product_reject_zero_valuation` at
   `before_validate` and again at the start of monkey-patched
   `StockEntry.validate` (after Server Scripts). Temporary
   `allow_zero_valuation_rate` + in-memory flag only.
3. **After Core** — `clear_core_auto_valuation_for_product_reject_bridge`
   undoes Core empty→VR auto-default.
4. **Allocator** — existing `allocate_scrap_absorbed_cost` (source/WIP pool).
5. **Assertion** — `assert_bridged_product_reject_priced` fails closed if the
   bridge allowed Core through but allocation did not price the row.

## Accounting

| Output | Bridge | Allocator | Rate source |
|--------|--------|-----------|-------------|
| Co-/By-Product / Additional FG | stage-equivalent | `allocate_stage_output_cost` | equivalent-unit pool |
| MAIN_PRODUCT_REJECT | product-reject | `allocate_scrap_absorbed_cost` | source/WIP absorbed pool |
| Component Scrap | scrap permit | issued-rate | consume issued rate |

Contract stamp for new Drafts: **`5.3.37`**. Historical stamps (`5.3.3`,
`5.3.34`, `5.3.35`, …) remain RIV-aware via version-at-least acceptance.

## Primary canary (`PO-JOB10094`)

```text
Consume 30100054 × 940 @ 738,730 → pool 694,406,200
FG 30100055 × 933 @ 738,730 → 689,235,090
Product Reject 30100055 × 7 @ 738,730 → 5,171,110
Residual / Stock Adjustment = 0
Destination scrap WH history irrelevant
```
