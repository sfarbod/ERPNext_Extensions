# erpnext_extensions 5.7.52

## Job Card Material Fulfillment — Original and Substitute Transfers

### Summary

Version **5.7.52** improves Job Card raw-material transfer completeness so that
a material requirement can be satisfied by any combination of:

- the originally requested item
- one approved substitute item
- multiple approved substitute items

Fulfillment now follows the same attribution model already used on Work Orders:
submitted Material Transfer for Manufacture lines are credited by
`original_item or item_code`. Original Job Card material rows and substitute
transfer history remain intact; submitted Stock Entries are not amended.

### Aggregation rule

For each original Job Card material requirement:

```text
effective_transferred_qty =
    original transfers
  + all valid substitute transfers
    attributed to that requirement on the same Job Card

shortfall_qty = max(0, required_qty − effective_transferred_qty)
```

Quantities are aggregated in stock UOM (`transfer_qty`). Draft and cancelled
Stock Entries are ignored. Material returns net out of the same attribution key.
Excess quantity on one requirement never offsets a different item.

Supported scenarios include original-only, substitute-only, mixed original and
substitute, multiple substitutes, partial transfers across several Stock
Entries, and over-transfer without negative outstanding quantities.

### Behaviour changes

| Area | Change |
|---|---|
| Move Materials progress | Treats materials as complete when effective transferred qty covers required qty |
| Submit validation | Uses the same effective shortfall calculation server-side |
| Shortage calculation | Remaining qty to move reflects uncovered demand after substitutes |
| Desk UI | Move Materials / Submit use the authoritative server-side status |

### Move Materials API and permissions

The substitute-aware shortage calculation is exposed through properly
whitelisted, permission-checked endpoints:

- Document method `Job Card.get_material_shortages` remains callable from Desk.
- Module API `get_material_shortages_for_job_card` validates the Job Card
  identifier and requires read permission on that document.
- Endpoints are read-only and do not create or submit Stock Entries.

This corrects a Method Not Allowed failure that occurred when the patched
shortage method was not registered in Frappe’s whitelist set.

### Traceability and safety

- Original material requirements remain visible on the Job Card.
- Substitute rows and Stock Entry `original_item` links preserve requested
  versus supplied history.
- No Job Card Item rows are deleted, renamed, or overwritten by this release.
- Submitted Stock Entries are not modified.
- Material validation is not globally bypassed.
- Manufacture consumption / backflush behaviour is unchanged.
- Attribution is scoped per Job Card to prevent cross-document bleed or
  double-counting.

### Tests

Regression coverage includes:

- original-only, substitute-only, and mixed fulfillment
- multiple substitutes and multi-entry partial transfers
- over-transfer, returns, and invalid unlinked substitutes
- cross–Job Card isolation
- Move Materials whitelist registration
- authorized access, unauthorized denial, missing/invalid identifiers
- read-only API behaviour (no document mutation)

### Files

- `stock_extensions/job_card_alternative_transfer/` (attribution, patch, API, tests)
- `stock_extensions/public/js/job_card_alternative_transfer.js`
- `iran_accounting/integration/monkey_patches.py`
- `hooks.py`
- `__init__.py` → `5.7.52`
