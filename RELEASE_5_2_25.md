# Release 5.2.25 — Scope-aware RIV valuation integrity (converted-data safe)

## Summary

Iran Accounting valuation integrity guards (I1/I2/I3/I4/I5) remain **fail-closed inside the declared RIV target item scope**, but no longer abort an entire Repost Item Valuation when ERPNext’s multi-hop `dependant_sle_voucher_detail_no` traversal encounters **legacy/converted SLE corruption on a different item**.

## Root cause addressed

Purchase Invoice → Purchase Receipt landed-cost adjustment (`set_landed_cost_based_on_purchase_invoice_rate`) correctly wrote `amount_difference_with_purchase_invoice`, then queued RIV. RIV for item `13100134` expanded through Manufacture FG cascades and hit poisoned FG `20100064` (negative `incoming_rate`). The global I1 guard aborted the RIV, leaving PR SLE/GL stale and an SRBNB residual equal to the unapplied amount difference.

## New scope rule

| Context | Behavior |
|---------|----------|
| Non-RIV submit / unknown targets | Fail-closed (unchanged) |
| RIV declared target item(s) — any warehouse | **BLOCK** on integrity violation (atomic target chain) |
| Different item reached only via dependant expansion | **LOG** (Error Log + flags) and **continue** vanilla `process_sle` |

Target items = `Repost Item Valuation.item_code` ∪ items in `items_to_be_repost`.  
Same Stock Entry membership alone does **not** expand blocking scope to sibling items that are not declared targets; SE-level asserts block only when the voucher **includes** a target item.

## Files

- `iran_accounting/domain/riv_valuation_scope.py` (new)
- `iran_accounting/domain/riv_valuation_guard.py` (scope-aware before/after/SE asserts)
- `iran_accounting/integration/monkey_patches.py` (`process_sle` binds engine on `frappe.local`)
- `iran_accounting/tests/test_riv_valuation_scope.py`

## Out-of-scope anomaly audit

Each non-blocking hit logs: RIV name, target items/warehouses, offending voucher/SLE/item/warehouse/batch, invariant code, and classification reason.

## Tests

`bench --site development.localhost run-tests --app erpnext_extensions --module erpnext_extensions.iran_accounting.tests.test_riv_valuation_scope`
