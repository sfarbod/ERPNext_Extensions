# Release 5.7.27 — Retrospective Finished Goods Cost Allocation

**Version:** **5.7.27**  
**Branch:** `develop`  
**Runtime scope:** Non-transactional monthly FG cost allocation by Persian business month  
**Site validated:** `development.localhost` — Company `اسپاد فارمد دارو`, FY **1405** Farvardin–Shahrivar  
**Deployment status:** Local Development only. **No Production push/merge/deploy without separate authorization.**

## Objective

Persist a three-level historical cost allocation calculation:

1. Monthly GL expense pools (user-configured Account × Cost Center × Department) + GL line snapshots  
2. Finished-goods production batches with month-wide normalized shares + Batch × Expense Pool  
3. Finished products within batches + Product × Expense Pool  

No Journal Entries, Stock Entries, GL postings, or valuation changes.

## Architecture

- Settings: `Finished Goods Cost Allocation Settings` + child `Expense Account Dimension Rule`  
- Parent: `Monthly FG Cost Calculation`  
- Details: `MFG Cost Expense Pool`, `MFG Cost GL Line`, `MFG Cost Batch Allocation`, `MFG Cost Batch Expense Allocation`, `MFG Cost Product Allocation`, `MFG Cost Product Expense Allocation`, `MFG Cost Calculation Revision`  
- Engine: `erpnext_extensions.iran_accounting.fg_cost_allocation`  
- Contract: `assert_consumable()` / retrieval helpers for a future absorption feature  

Batch share formula:

`Batch Share = Batch Normalized Qty / Σ(all eligible FG batches in the Persian month)`

Blank Settings dimensions match blank GL dimensions only (never “all”).

## Dev validation notes (FY1405)

- Settings created for `اسپاد فارمد دارو` with `normalization_uom=سرنگ` and **zero expense rules** (user must supply Account/CC/Dept rows; investigation accounts are not hardcoded).  
- All six months calculated production batches/products; status **Incomplete** due to missing Item UOM conversions to `سرنگ` (notably `BOX(1pfs)` items).  
- With no configured rules: pool / Batch×Pool / Product×Pool counts are 0; expense allocation cannot complete until rules are entered.  
- `assert_consumable()` correctly rejects Incomplete calculations.

## Tests

`iran_accounting/tests/test_fg_cost_allocation_v5727.py` — month-wide shares, manual override (no silent renorm), multi-product residual allocation, consumable gate, GL line sum.

## Production

Unauthorized.
