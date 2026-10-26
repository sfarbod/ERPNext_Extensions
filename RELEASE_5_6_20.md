# erpnext_extensions 5.6.20

## Facility Management — Debt Purchase PDC Allocation & Schedule Totals

### Summary
Adds a dedicated Debt Purchase PDC allocation table on Facility, enforces
global PDC uniqueness and Draft→Active amount equality, filters Facility
Repayment PDC selection to allocated cheques only, and shows read-only summary
totals under the Repayment Schedule and Debt Purchase cheque grids.

### Feature 1 — Debt Purchase PDC Allocation
- **Criterion:** `Facility Type.is_debt_purchase = 1`.
- **Facility flag:** hidden read-only Check `is_debt_purchase`
  (`fetch_from = facility_type.is_debt_purchase`), synced on `validate` for
  reliable form `depends_on`.
- **Child DocType:** `Facility PDC Allocation` on Facility field `debt_purchase_pdcs`.
- **Section label (English source):** `Debt Purchase Cheques`
  (`depends_on = eval:cint(doc.is_debt_purchase)==1`).
  Persian via `translations/fa.csv`. Table field label left empty to avoid
  duplicate headings.
- **Columns (compact grid labels):** PDC, Cheque No, Due Date, Bank, Amount,
  Status (PDC remains source of truth; display fields read-only).
- **Summary bar (UI):** Cheque Count, Cheque Total, Principal, Profit,
  Facility Total, Difference — compact horizontal layout; Difference shows
  matched/mismatch text (not color-only) with integer-safe IRR math.
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
- **Copy:** `debt_purchase_pdcs` has `no_copy=1`.

### Feature 2 — Repayment Schedule Summary
- Presentation-only summary under `repayment_schedule`:
  Installment Count, Total Principal, Total Profit, Total Installments.
- Updates on add/remove/edit/refresh. Does not mutate stored amounts.
- Shared helper: `facility_management/public/js/facility_grid_footer.js`.
- Styles: `public/css/facility_grid_footer.css` (Frappe CSS variables).

### Localization
- All custom summary labels use English source strings with `__()`;
  Persian translations live in `erpnext_extensions/translations/fa.csv`.
- DocType/section/field labels use English source text (not hardcoded Persian).
- English user language → English UI; Persian → Persian UI + RTL from Frappe.

### UI visibility fix (earlier on same branch)
1. PDC section visibility driven by synced `is_debt_purchase` `depends_on`.
2. Summaries attach to field `$wrapper` (outside `grid.wrapper`) so grid rebuilds
   cannot wipe them.

### UI/UX redesign (this follow-up)
- Removed large grey summary boxes.
- Compact responsive summary bars (flex; 1440 / 1280 / 768 verified).
- Difference indicator: `matched` / `mismatch` text + subtle warn/ok styling.
- Removed duplicate Debt Purchase section/table labels.
- Shortened PDC child column labels for grid readability.

### Migration notes
- New child DocType `Facility PDC Allocation` with `UNIQUE KEY post_dated_cheque`.
- New Facility Table field `debt_purchase_pdcs`.
- New Facility Check field `is_debt_purchase` (hidden, fetched).
- No historical backfill of allocations.
- No Production changes in this release package.

### Test Facilities (development.localhost)
| Facility ID | Facility Type | Status | PDC Count | PDC Total | Schedule Rows | Result |
|---|---|---|---:|---:|---:|---|
| FAC-2026-13568 | E2E Debt Purchase Type | Draft | 3 | 9,900,000 | 3 | Exact allocation; UI OK |
| FAC-2026-13569 | E2E Debt Purchase Type | Active | 3 | 6,600,000 | 3 | Activated via receipt JE |
| FAC-2026-13570 | UI Test Normal Type | Draft | 0 | 0 | 3 | PDC hidden; schedule OK |
| FAC-2026-13571 | E2E Debt Purchase Type | Draft | 2 | 2,000,000 | 3 | Diff warning; activation blocked |
| FAC-2026-13572 | E2E Debt Purchase Type | Draft | 0 | 0 | 3 | Duplicate PDC rejected |

### Browser verification (Playwright)
- Screenshots: `sites/development.localhost/public/files/ui_ux_5620/`
  - `en_dp.png`, `en_normal.png`, `fa_dp.png`, `fa_normal.png`
  - `en_dp_1440.png`, `en_dp_1280.png`, `en_dp_768.png`
  - `fa_incomplete_diff.png`
- English: no Persian leakage in custom summaries; compact bars visible.
- Persian: translated labels; RTL from Desk; difference `عدم تراز` on incomplete.
- Responsive: no summary overflow at 1440 / 1280 / 768.

### Tests (development.localhost)
| Suite | Result |
|---|---|
| `test_facility_pdc_allocation` | 22 PASS (incl. localization / meta UI guards) |
| `test_facility_debt_purchase_e2e` | 6 PASS |
| `test_facility_debt_purchase_rejections` | 12 PASS |
| `test_facility_debt_purchase_repayment` | 2 PASS |
| `test_facility_integrity_guards` | 28 PASS |

### Ops applied (development only)
- `bench --site development.localhost migrate`
- `bench --site development.localhost clear-cache` / `clear-website-cache`
- Translation cache refreshed; `app_include_css` serves `facility_grid_footer.css`

### Compatibility
- Non-Debt Purchase Facilities/repayments unchanged.
- Historical submitted repayments remain readable/cancellable.
- Frappe / ERPNext v16 compatible; no core modifications.
