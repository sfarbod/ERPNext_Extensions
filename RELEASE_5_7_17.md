# Release 5.7.17 — Facility PDC Allocation cheque amount DECIMAL(30,9)

**Version:** 5.7.17  
**Branch:** `develop`  
**Runtime scope:** Facility Management / Debt Purchase PDC allocation  
**Deployment status:** Local Development release preparation. **No Production deploy. No push.**

## Bug Fix — Facility PDC Allocation Amount Precision

### Problem

The database column `tabFacility PDC Allocation.cheque_amount` was defined as
`DECIMAL(21,9)`, allowing only **12** integer digits. Saving a Facility with a
large allocated PDC amount such as `1,500,000,000,000` IRR caused:

```text
MySQLdb.DataError: (1264, "Out of range value for column 'cheque_amount' at row 1")
```

Parent `Facility` monetary columns and `Post Dated Cheque.cheque_amount` were
already `DECIMAL(30,9)`. Only the fetched child-table copy on
`Facility PDC Allocation` had been left on the default Frappe Currency width
after that child DocType was introduced.

### Resolution

`cheque_amount` was expanded to `DECIMAL(30,9)` and made persistent across
metadata sync and migrate:

1. DocType JSON field `length` set to `30` on
   `Facility PDC Allocation.cheque_amount`.
2. Property Setter `Facility PDC Allocation-cheque_amount-length` = `30`
   (prevents `updatedb()` narrowing back to `21,9`).
3. `facility_precision.TABLES_AND_COLUMNS` allowlist updated to include
   `tabFacility PDC Allocation.cheque_amount`.
4. Pre-model-sync patch:
   `set_facility_pdc_allocation_cheque_amount_decimal_metadata`.
5. Post-model-sync idempotent ALTER patch:
   `expand_facility_pdc_allocation_cheque_amount_precision`.
6. `decimal_precision_after_migrate` now runs
   `repair_facility_decimal_schema()` so every migrate re-asserts Facility
   monetary columns (including this field).
7. Regression suite
   `test_facility_pdc_allocation_cheque_amount_precision` covers schema,
   idempotency, non-narrowing after `updatedb()`, production-sized amount
   persistence, and allowlist coverage.

### Explicitly unchanged

This release does **not** change:

- accounting documents / Journal Entries / GL Entries
- Facility repayment calculation formulas
- Debt Purchase allocation business rules
  (`Allocated PDC Total = Facility Principal + Facility Profit`)
- PDC workflow or duplicate-allocation protection logic

## Verification Evidence

| Item | Result |
|------|--------|
| Facility | `FAC-2026-07063` |
| PDC | `PDC-2026-00359` |
| Persisted amount | `1500000000000.000000000` |
| Allocation rows for this PDC | **1** (no duplicate) |
| Contract check | `principal + profit = allocated = 1,500,000,000,000` |
| Column after migrate | `decimal(30,9)` |
| Dedicated tests | **5/5 passed** |

Fix commit (preserved): `13f018b86441e8387b99ac1c067bd65d9d1d04bb`

## Version

`erpnext_extensions.__version__` → **5.7.17**
