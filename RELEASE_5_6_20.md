# erpnext_extensions 5.6.20

## Facility Management — Debt Purchase PDC Allocation & Schedule Totals

### Summary
Adds a dedicated Debt Purchase PDC allocation table on Facility, enforces
global PDC uniqueness and Draft→Active amount equality, filters Facility
Repayment PDC selection to allocated cheques only, and shows read-only footer
totals on the Repayment Schedule grid.

### Feature 1 — Debt Purchase PDC Allocation
- **Criterion:** `Facility Type.is_debt_purchase = 1`.
- **Facility flag:** hidden read-only Check `is_debt_purchase`
  (`fetch_from = facility_type.is_debt_purchase`), synced on `validate` for
  reliable form `depends_on`.
- **Child DocType:** `Facility PDC Allocation` on Facility field `debt_purchase_pdcs`.
- **Section label:** «چک‌های خرید دین»
  (`depends_on = eval:cint(doc.is_debt_purchase)==1`).
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

### UI fix (Desk visibility — follow-up on same 5.6.20 branch)
**Root cause (why features were not visible after d190dfe):**

1. **PDC table:** Section visibility relied on async `frappe.db.get_value` +
   `toggle_display`, racing Form `depends_on` (`eval:doc.facility_type`). That
   was fragile and could leave the section hidden or inconsistently shown.
   Desk now drives visibility from synced `doc.is_debt_purchase` via
   `depends_on`.
2. **Schedule / PDC footers:** Footers were attached inside `grid.wrapper`
   (before `.grid-footer`). Frappe `grid.make()` / `refresh` rebuilds wipe that
   DOM, so footers disappeared after render. Footers now attach to the field
   `$wrapper` (outside the grid rebuild zone) and are re-applied after refresh
   with short delayed retries. Parent form handlers cover child add/remove.

**Corrective files:** `facility.json`, `facility.js`, `facility.py`,
`facility_grid_footer.js`.

### Migration notes
- New child DocType `Facility PDC Allocation` with
  `UNIQUE KEY post_dated_cheque`.
- New Facility Table field `debt_purchase_pdcs`.
- New Facility Check field `is_debt_purchase` (hidden, fetched).
- Existing Facilities backfilled from Facility Type on migrate/verify.
- No historical backfill of allocations.
- No Production changes in this release package.

### Test Facilities (development.localhost)
| Facility ID | Facility Type | Status | PDC Count | PDC Total | Schedule Rows | Result |
|---|---|---|---:|---:|---:|---|
| FAC-2026-13568 | E2E Debt Purchase Type | Draft | 3 | 9,900,000 | 3 | Exact allocation; UI OK |
| FAC-2026-13569 | E2E Debt Purchase Type | Active | 3 | 6,600,000 | 3 | Activated via receipt JE; editable |
| FAC-2026-13570 | UI Test Normal Type | Draft | 0 | 0 | 3 | PDC hidden; schedule footer OK |
| FAC-2026-13571 | E2E Debt Purchase Type | Draft | 2 | 2,000,000 | 3 | Diff shown; activation blocked |
| FAC-2026-13572 | E2E Debt Purchase Type | Draft | 0 | 0 | 3 | Duplicate PDC rejected |

### Browser verification (Playwright headless Chromium)
- Base: `http://development.localhost:8000`
- Screenshots:
  `sites/development.localhost/public/files/ui_verify_5620/`
  (`draft_dp_pdc_area.png`, `active_dp_pdc_area.png`, `normal_schedule_area.png`, …)
- PDC table visible (DP): **YES**
- PDC footer visible (DP): **YES**
- Schedule footer visible: **YES**
- Active editing (`read_only=0`): **YES**
- Normal Facility hides PDC: **YES**
- Footer refresh after add-row: **YES**
- Console: only unrelated socket.io Unauthorized / incidental 404 (no Facility JS errors)

### Tests (development.localhost)
| Suite | Result |
|---|---|
| `test_facility_pdc_allocation` | PASS (incl. UI meta regression) |
| `test_facility_debt_purchase_e2e` | 6 PASS |
| `test_facility_debt_purchase_rejections` | 12 PASS |
| `test_facility_debt_purchase_repayment` | 2 PASS |
| `test_facility_integrity_guards` (5.6.19 regression) | 28 PASS |

### Ops applied (development only)
- `bench --site development.localhost migrate`
- `bench --site development.localhost clear-cache`
- `bench --site development.localhost clear-website-cache`
- `is_debt_purchase` column + meta verified; FormMeta embeds footer JS

### Compatibility
- Non-Debt Purchase Facilities/repayments unchanged.
- Historical submitted repayments remain readable/cancellable.
- Frappe / ERPNext v16 compatible; no core modifications.
