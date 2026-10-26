# erpnext_extensions 5.6.20

## Facility Management — Debt Purchase PDC Allocation & Schedule Totals

### Summary
Adds a dedicated Debt Purchase PDC allocation table on Facility, enforces
global PDC uniqueness and Draft→Active amount equality, filters Facility
Repayment PDC selection to allocated cheques only, and shows read-only footer
totals on the Repayment Schedule grid.

### Feature 1 — Debt Purchase PDC Allocation
- **Criterion:** `Facility Type.is_debt_purchase = 1` (no redundant Facility flag).
- **Child DocType:** `Facility PDC Allocation` on Facility field `debt_purchase_pdcs`.
- **Section label:** «چک‌های خرید دین» (shown for Debt Purchase types via client).
- **Columns (PDC is source of truth; display fields read-only):**
  Post Dated Cheque, Cheque Number, Cheque Due Date, Drawer Bank, Cheque Amount,
  Cheque Status.
- **Footer (UI):** cheque count, allocated total, Facility principal/profit/total,
  difference `(allocated − facility total)` using IRR integer-safe arithmetic.
- **Draft:** incomplete allocation may save (difference shown, not blocking).
- **Activation (Draft→Active and receipt-JE activation API):** allocated PDC
  total must **exactly** equal `principal + profit` (Decimal comparison). Persian
  validation message shows facility total, allocated total, and difference.
- **Historical Active:** empty allocation table does **not** block unrelated
  edits; Finance may populate while Active.
- **Uniqueness:** unique DB index on `post_dated_cheque`; server validation;
  MariaDB `GET_LOCK` per PDC during allocation (`fac_pdc:` prefix).
- **Closed:** allocation edits blocked.
- **Remove:** blocked if a submitted Facility Repayment uses that PDC.
- **Repayment:** `debt_purchase_cheque_query` filters by Facility allocation;
  `validate_debt_purchase_cheque_repayment` rejects unallocated PDCs.
- **Accounting:** allocation is master data only — no JE/GL/repayment side effects.
  Settlement links (`debt_purchase_facility` / `debt_purchase_repayment`) remain
  separate. Existing `PDC Allocation` DocType unchanged.
- **Copy:** `debt_purchase_pdcs` has `no_copy=1`.

### Feature 2 — Repayment Schedule Footer
- Presentation-only footer under `repayment_schedule`:
  installment count, sum principal, sum profit, sum installments
  (`principal + profit`).
- Updates on add/remove/edit/refresh. Does not mutate stored amounts.
- Shared helper: `facility_management/public/js/facility_grid_footer.js`.

### Migration notes
- New child DocType `Facility PDC Allocation` with
  `UNIQUE KEY post_dated_cheque`.
- New Facility Table field `debt_purchase_pdcs`.
- No historical backfill of allocations.
- No Production changes in this release package.

### Tests (development.localhost)
| Suite | Result |
|---|---|
| `test_facility_pdc_allocation` | 17 PASS |
| `test_facility_debt_purchase_e2e` | 6 PASS |
| `test_facility_debt_purchase_rejections` | 12 PASS |
| `test_facility_debt_purchase_repayment` | 2 PASS |
| `test_facility_integrity_guards` (5.6.19 regression) | 28 PASS |

### Compatibility
- Non-Debt Purchase Facilities/repayments unchanged.
- Historical submitted repayments remain readable/cancellable.
- Frappe / ERPNext v16 compatible; no core modifications.
