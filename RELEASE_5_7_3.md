# erpnext_extensions 5.7.3

## Stock Entry ledger contract — Manufacture amount authority vs SABE qty×rate

### Production blockers

Manufacture submit blocked by `enforce_stock_entry_ledger_contract` (validator unchanged):

| Document | Row delta | Aggregate delta |
|----------|-----------|-----------------|
| MAT-STE-2026-43598 | +13 IRR | +13 IRR |
| MAT-STE-2026-43604 | −211 IRR | −211 IRR |
| MAT-STE-2026-43606 | −4 IRR | −4 IRR |

### Root cause

SABE integerization overwrote SLE `stock_value_difference` with `qty × ROUND_HALF_UP(rate)` after SED→SLE sync. Capitalization-aware **SED.amount** (including additional_cost / LCV / intentional amount−rate×qty residual) remained the document authority, so the ledger mirror failed. Aggregate mismatch equals the same single FG row delta.

### Fix

1. After SABE integerization on Manufacture / Repack / Material Consumption for Manufacture, restore SLE movement from SED.amount via `reassert_document_amount_authority_sle` (idempotent).
2. Do **not** queue SABE A2 merely to absorb intentional document-amount residuals.
3. Incoming Manufacture SED refresh: SLE magnitude already includes addl/LCV — do **not** add them again onto `amount` (prevents double-count capitalization).

### Explicitly not changed

- Ledger contract validator remains fully enabled (no tolerance increase or bypass).
- Integer IRR + `ROUND_HALF_UP` policies remain intact.
- No artificial Journal Entries, Stock Adjustments, or Round Off postings.
- Purchase Receipt, Material Receipt, Delivery Note, and non–document-authority Stock Entry paths unchanged.

### Tests

`test_se_ledger_amount_authority_v573.py` (8 cases)

### Version

`erpnext_extensions.__version__` → **5.7.3**
