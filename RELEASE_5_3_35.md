# Release 5.3.35 — Stage-Equivalent By-Product Costing

## Summary

Qualifying Manufacture Job Card **By-Products** (and other
`STAGE_SECONDARY_TYPES`) can fail Draft creation in ERPNext Core with
**Valuation Rate Missing** before Iran equivalent-output costing runs.

This release adds a **narrow pre-Core bridge** so proven stage-equivalent
outputs survive Core validation, then **reuses** the existing authoritative
allocator:

```text
allocate_stage_output_cost  (CLASS_CO_PRODUCT joint pool)
```

No separate `by_product_equal_rate()` engine was introduced.

Target canary: Job Card `PO-JOB07352` → Manufacture Draft → Submit (rollback).

## Problem

Lifecycle ordering:

```text
StockEntry.save
  → Core validate
      → set_bomless_secondary_valuation_types  (empty → Valuation Rate)
      → set_basic_rate / get_valuation_rate
      → Valuation Rate Missing
  → Iran validate never reached
      → allocate_stage_output_cost never ran
```

## Root cause

ERPNext Core auto-defaults bomless secondaries to `valuation_type = Valuation Rate`.
Iran stage costing runs later. Qualifying By-Products never reached the joint
equivalent-output pool.

## Fix

1. **Qualification** — fail-closed `is_stage_equivalent_output_candidate`
   (Manufacture, IRR contract, stage secondary type, JC/BOM origin, qty,
   no explicit Manual / Valuation Rate / % Component Cost).
2. **`before_validate` bridge** — `permit_stage_equivalent_zero_valuation`
   sets an in-memory flag + temporary `allow_zero_valuation_rate` (same pattern
   as scrap). Does **not** assign the economic rate.
3. **After Core** — `clear_core_auto_valuation_for_stage_bridge` undoes Core’s
   empty→VR auto-default so finance-exclusion does not steal the row.
4. **Allocator** — existing `allocate_stage_output_cost` prices the joint pool.
5. **Assertion** — `assert_bridged_stage_outputs_priced` fails closed if the
   bridge allowed Core through but allocation did not price the row.

## Factor `0.0` contract

Frappe Float custom fields serialize unset values as `0.0` (Custom Field default
is `None`; site JC/SE rows are overwhelmingly literal `0.0` with **zero**
positive factors). Runtime domain boundary:

| Stored | Semantics |
|--------|-----------|
| `NULL` / empty / `0.0` | UNSET → physical UOM / default factor `1` |
| `> 0` | explicit multiplier |
| `< 0` | invalid → fail closed |

Do **not** silently treat explicit zero as `1` beyond this unset serialization.

## Accounting

- One authoritative equivalent-output model (Co-Product / By-Product / Additional FG).
- Explicit Manual / Valuation Rate / % Component Cost remain finance-excluded.
- Nonqualifying By-Products still hit normal Core Valuation Rate safety.
- Different UOM requires valid conversion or positive factor (no 1:1 assumption).
- Component Scrap / Product Reject / TYPE A / B / C remain separate.
- Contract stamp for new Drafts: **`5.3.35`**. Historical stamps (`5.3.3`,
  `5.3.34`, …) remain RIV-aware via version-at-least acceptance.

## Primary canary (`PO-JOB07352`) — live restored DB

| Metric | Value |
|--------|------:|
| Consumed pool | 2,313,749,853 |
| Component Scrap | 34,614,988 |
| Distributable pool | 2,279,134,865 |
| FG eq qty (882 BOX × 2) | 1,764 سرنگ |
| By-Product eq qty | 1 سرنگ |
| Common eq rate | ≈ 1,291,294.54 |
| By-Product 30500006 rate/amount | **1,291,295** |
| FG 20100064 rate | **2,582,589** / BOX |
| FG amount | **2,277,843,570** |
| Material residual | **0** |
| Stock Adjustment | **0** |
| UOM check | `FG rate ≈ 2 × BY rate` (≤1 IRR) |

Investigation’s earlier ~1,286,057 figure omitted live Component Scrap deduction;
live Draft economics from `make_stock_entry_for_semi_fg_item` are authoritative.

### Submit (rollback-controlled)

- `docstatus=1`, SLE=25, GL balanced (debit=credit=2,313,749,853)
- Stock Adjustment debit = **0**
- row↔SLE exact (no mismatches)
- Cancel → SLE/GL live counts = 0; transaction rolled back; Draft deleted

## Safety / regressions

| Suite | Result |
|-------|--------|
| `test_stage_equivalent_byproduct_bridge_v5335` (40) | OK |
| `test_stage_output_equivalent_costing` (33) | OK |
| `test_component_scrap_issued_rate` (27) | OK |
| `test_manufacture_irr_residual_v5334` (43) TYPE C | OK |
| `test_manufacture_residual_submit_v5330` (14) TYPE B | OK |
| New-logic coverage (bridge helpers) | Statements **100%** / Branches **100%** / Missing **NONE** |

## Playwright

`manufacture-stage-byproduct-v5335.spec.ts` — P01–P08 Job Card → Draft path,
By-Product non-zero stage rate, UOM equivalence, zero residual, cleanup.

## Deployment

```text
Push: NO
Tag: NO
Production deployment: NO
```
