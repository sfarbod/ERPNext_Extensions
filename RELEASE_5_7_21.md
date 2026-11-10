# Release 5.7.21 — Manufacturing monetary DECIMAL(30,9) (BOM / Work Order / Job Card)

**Version:** **5.7.21**  
**Branch:** `develop`  
**Scope:** Database storage precision for manufacturing monetary fields  
**Deployment status:** Local Development. **No Production deploy. No push.**

## Objective

Audit and standardize monetary / financial amount columns used by **BOM**,
**Work Order**, **Job Card**, and their related child tables to:

```text
DECIMAL(30,9)
```

UI currency display precision and company rounding settings are unchanged.
Quantities, conversion factors, percentages, times, potencies, and volumes are
**not** widened.

## Problem

Frappe default Currency/Float storage is typically `DECIMAL(21,9)` (and some
hour-rate columns were `DECIMAL(21,2)`). Large IRR manufacturing costs and
9-decimal intermediate rates can overflow or lose scale on persist. Prior
`erpnext_extensions` DECIMAL(30,9) work covered selling, purchasing, stock
repost, facility, and GL — **not** the manufacturing graph.

## Field inventory (hardened)

| DocType | Monetary fields → DECIMAL(30,9) |
|---------|----------------------------------|
| BOM | `operating_cost`, `operating_cost_per_bom_quantity`, `raw_material_cost`, `scrap_material_cost`, `secondary_items_cost`, `total_cost`, `base_*` counterparts, `cost_allocation` |
| BOM Item | `rate`, `base_rate`, `amount`, `base_amount` |
| BOM Explosion Item | `rate`, `amount` |
| BOM Operation | `hour_rate`, `base_hour_rate`, `operating_cost`, `base_operating_cost`, `cost_per_unit`, `base_cost_per_unit` |
| BOM Scrap Item | `rate`, `base_rate`, `amount`, `base_amount` |
| BOM Batch Items | `rate`, `base_rate`, `amount`, `base_amount` |
| BOM Secondary Item | `cost`, `base_cost`, `rate` (SQL column when present) |
| BOM Creator / Item | `raw_material_cost`; item `rate`/`amount`/`base_*` |
| Work Order | `planned_operating_cost`, `actual_operating_cost`, `additional_operating_cost`, `total_operating_cost`, `corrective_operation_cost` |
| Work Order Item | `rate`, `amount` |
| Work Order Operation | `hour_rate`, `planned_operating_cost`, `actual_operating_cost` |
| Work Order Additional Item | `rate`, `amount` (when DocType present) |
| Job Card | `hour_rate` |

**Excluded (examples):** qty / stock_qty / transferred_qty / process_loss_qty,
`conversion_rate` / `plc_conversion_rate` / `conversion_factor`, percentages,
`time_in_mins`, batch sizes, potency/volume custom Floats, Job Card item
returnable qty customs.

Previous column types observed on Development: mostly `decimal(21,9)`;
`BOM Operation.hour_rate` was `decimal(21,2)`. Target for all allowlisted
fields: `decimal(30,9)`.

Note: `tabBOM Scrap Item` may exist as an orphan SQL table without a live
DocType on some sites; the patch still ALTERs its monetary columns when
present. `Work Order Additional Item` is allowlisted when the DocType/table
exists and is skipped cleanly when the table is absent.

## Implementation

| Layer | Path |
|-------|------|
| Registry + repair | `manufacturing_decimal_precision_v5721.py` |
| Pre-model-sync | `patches/pre_model_sync/set_manufacturing_amount_decimal_metadata_v5721.py` |
| Post-model-sync | `patches/post_model_sync/expand_manufacturing_amount_precision_v5721.py` |
| Drift heal | `decimal_precision_after_migrate.after_migrate` (+ manufacturing) |
| Tests | `tests/test_manufacturing_decimal_precision_v5721.py` |

Mechanics:

- Property Setter `length=30` (DECIMAL width)
- Property Setter `precision=9` (Frappe's name for DECIMAL **scale** — required
  because some fields such as `BOM Operation.hour_rate` ship with DocField
  `precision=2`, which would otherwise make `updatedb()` emit `DECIMAL(30,2)`)
- Idempotent `ALTER … MODIFY DECIMAL(30,9)` preserving NULL/default
- Manufacturing-specific decide also widens scale 2→9 safely
- Completeness guard classifies every Currency/Float/Percent on the audited graph

## Dependencies / impact

- Owned fields: BOM / Work Order / Job Card graph only.
- Downstream stock valuation / SLE / GL already covered by prior precision
  modules; this release does **not** retarget those tables.
- No change to manufacturing controllers, cost formulas, or Iran Accounting
  integer IRR rules beyond storage capacity.

## Data safety

- Widening DECIMAL precision/scale does not truncate existing values.
- Patch skips already-correct `DECIMAL(30,9)` columns.
- Re-running migrate / repair is a no-op when schema is healthy.

## Tests

`test_manufacturing_decimal_precision_v5721` — schema/idempotency/completeness,
updatedb non-narrow, `123456789.123456789` + large IRR round-trip on BOM /
Work Order / Job Card monetary columns.

## Version

`erpnext_extensions.__version__` → **5.7.21**
