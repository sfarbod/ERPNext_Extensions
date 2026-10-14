# Release 5.5.44 — Prevent New Manufacturing and Batch Valuation Defects

**Version:** 5.5.44  
**Date:** 2026-10-13  
**Previous:** 5.5.43  
**Branch:** `develop`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (unmodified Core)

**Scope:** Prevention only for **new** IRR Stock Entries. No historical source repair. No historical Repost Item Valuation. No bulk RIV. No production changes.

---

## A. Summary

1. **Manufacturing double-capitalization detector** — After Iran SAME_ITEM_MULTI_FG / STAGE_CO, block same-item multi-FG rows that each still absorb ≈ the full consumed pool (Core multi-FG defect). Historical unstamped submitted vouchers remain untouched (`uses_v533_contract` gate).
2. **Closed manufacture contract restore on submit** — Core `calculate_rate_and_amount` → `set_basic_rate` rewrites every finished row as `outgoing / row_qty` during validate on submit, destroying a previously closed SAME_ITEM_MULTI_FG / STAGE_CO / OUTPUT_FAMILY plan. `patched_set_total_incoming_outgoing_value` re-applies `apply_iran_manufacture_output_contract` when allocation is already closed so residual polish / R1 sees correct economics. Approved STAGE_CO / SAME_ITEM contracts are unchanged.
3. **Batch valuation identity** — Before submit, require an existing Serial and Batch Bundle or `use_serial_batch_fields=1` for batchwise rows. Do not treat Stock Settings `auto_create_serial_and_batch_bundle_for_outward` + populated `batch_no` as a safe path (Core rejects that combination). Post-submit ledger contract fails if SLE has no batch/bundle for a batchwise SED.
4. **Integer IRR Serial and Batch Entry** — New documents must not persist fractional SABE rates/amounts (process_sle guard + post-submit check). RIV path is skipped.
5. **Unapproved indivisible MA remainder** — Full-depletion outgoing moves where integer pool is not divisible by quantity are rejected with item/warehouse/batch/qty/value/rate/remainder diagnostics. No automatic Stock Adjustment.
6. **Source-linked custom returns** — When `custom_is_returned` + `custom_reference_no` are set, return rates/amounts must match the source issue. Ordinary Material Receipts are unchanged.

---

## B. Changed valuation paths

| Path | Change |
|------|--------|
| `validate_stock_entry` | `run_validate_prevention_guards` (manufacture double-capital) |
| `patched_set_total_incoming_outgoing_value` | restore closed manufacture contract after Core `set_basic_rate` |
| `before_submit_stock_entry` | batch identity, return valuation, indivisible MA remainder |
| `enforce_stock_entry_ledger_contract` | post-submit batch identity + integer SABE |
| `update_serial_batch_no_valuation` (monkey patch) | fail-closed on fractional IRR SABE for non-RIV |

---

## C. Unchanged (preserved)

- STAGE_CO rate-authoritative composition and TYPE C Stock Adjustment (5.5.43)
- SAME_ITEM_MULTI_FG amount-first / R2 representation contract (5.5.19+)
- Product Reject bridge, by-product / co-product allocators
- Native outgoing SLE authority for Issue / Transfer (5.5.40+)
- Historical unstamped Manufacture documents under RIV
- ERPNext Core (no Core edits)

---

## D. Migration requirements

None. No patches. No schema changes. No data migration.

---

## E. Known limitations

1. **Indivisible Moving Average remainder** — Accounting treatment is **not** approved. New full-depletion submits that would leave an indivisible integer remainder are **blocked**, not auto-adjusted.
2. **Historical documents** — Pre-existing double capitalization (e.g. `MAT-STE-2026-29742`), divergent custom returns (e.g. `MAT-STE-2026-31063`), and fractional SABE rows are **not** repaired by this release.
3. **Batch identity** — Rows with `use_serial_batch_fields=1` still rely on Core `make_bundle_using_old_serial_batch_fields` on submit. `auto_create` + `batch_no` without `use_serial_batch_fields` is fail-closed. Post-submit contract catches silent warehouse MA fallback.
4. **Custom return fields** — Only enforce when both `custom_is_returned` and `custom_reference_no` are set. They are not a native ERPNext return doctype.

---

## F. Historical repair exclusions

Do **not** use this release to:

- Cancel/amend historical Manufacture vouchers
- Revalue historical Material Receipt returns
- Drain the Repost Item Valuation queue
- Silently reinterpret unstamped submitted Manufacture documents

A separate historical repair campaign is required after prevention is verified in production.

---

## G. Tests

Module: `erpnext_extensions.iran_accounting.tests.test_prevention_v5544`

Regression smoke (unchanged contracts):

- `test_manufacture_output_contract_v5519` (SAME_ITEM_MULTI_FG)
- `test_zero_fg_stage_allocation_v5543` (STAGE_CO)
- `test_stage_secondary_r2_ledger_v5543`

---

## H. Verification (development.localhost)

| Check | Result |
|-------|--------|
| Historical `MAT-STE-2026-29742` unmodified | PASS |
| Historical `MAT-STE-2026-31063` unmodified | PASS |
| RIV queue untouched (74 Completed / 257 Queued / 18 Skipped) | PASS |
| Scheduler disabled | PASS |
| Unrelated `cancelled_voucher_repair.py` preserved | PASS |
