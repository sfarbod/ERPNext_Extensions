# erpnext_extensions 5.5.32

## Selective Manufacture manual-rate unlock

This release adds an optional row filter to the existing bench-only
Manufacture manual-rate unlock utility. When `row_name` and `item_code`
are supplied, only that incoming output row can be unlocked. Omitting
both parameters keeps the v5.5.31 document-wide behavior.

It does not change Iran Accounting valuation formulas, does not start
Repost Item Valuation, and does not unlock historical documents by itself.

### Added

- Optional `row_name` and `item_code` arguments on
  `unlock_manufacture_manual_rates`.
- Fail-closed checks: `row_name` requires a submitted Manufacture
  `stock_entry`; `item_code` requires `row_name`; a missing row, a row
  from another document, or an item mismatch unlocks nothing.
- Dry-run reporting for the selected row, excluded eligible rows, and
  the metadata write set.
- Apply revalidates the selected row immediately before writing.

### Preserved

- Unfiltered unlock of every eligible output row when no filter is passed.
- BULK_SCRAP manual valuation policy.
- Existing Iran Accounting valuation formulas.
- Quantities, warehouses, batches, rates, amounts, SLE, and GL during
  the metadata unlock.
- Native ERPNext RIV. This utility still does not create or execute RIV.

### Deployment Notes

- **Upgrade:** install / update `erpnext_extensions` to **5.5.32**.
- **`bench migrate`:** not required. No DocType or patch is added.
- **Historical unlock:** not automatic. A selective apply is an explicit
  bench call.
- **RIV:** not automatic. After a selective unlock, create only the
  targeted native Item-and-Warehouse RIVs the operator has approved.
- Production is not updated by preparing this release.

### Production command for the first row

Dry-run:

```bash
bench --site erp.espadpharmed.com execute \
  erpnext_extensions.iran_accounting.unlock_manufacture_manual_rates.unlock_manufacture_manual_rates \
  --kwargs '{"stock_entry":"MAT-STE-2026-24883-1","row_name":"aelcfduknt","item_code":"30500006","dry_run":1}'
```

Apply only after that dry-run reports `selected_unlock_count` 1 for
`30500006` and `excluded_unlock_count` 1 for `13200187`:

```bash
bench --site erp.espadpharmed.com execute \
  erpnext_extensions.iran_accounting.unlock_manufacture_manual_rates.unlock_manufacture_manual_rates \
  --kwargs '{"stock_entry":"MAT-STE-2026-24883-1","row_name":"aelcfduknt","item_code":"30500006","dry_run":0}'
```
