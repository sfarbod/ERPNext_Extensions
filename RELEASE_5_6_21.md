# erpnext_extensions 5.6.21

## Account Explorer — Unified Party Analysis, Analyze Navigation, Performance

### Summary
Restores a Production database snapshot into Development, corrects Party Name /
Identifier Field display, adds Related Roles and Unified Party Analyze with
per-role financial isolation, makes Analyze navigation consistent across axes,
removes expand/collapse drill icons from all axes, and optimizes Account Explorer
first-load performance on large GL volumes.

**Follow-up (same 5.6.21):** Fixes release-blocking defect where Analyze buttons
were visible but clicks did nothing in the real UI.

**Version remains 5.6.21** (no bump).

### Analyze click root cause (P0 functional fix)
**Symptom:** Analyze rendered on Parties / Account Levels / etc., but clicking
produced no panel, no API call, and no Analysis Path change.

**Root cause:** DataTable delegated `.ae-voucher-action` clicks for non-voucher
axes fell through to `handle_voucher_row_action()`, which **intentionally
no-ops `action === "analyze"`** (Vouchers must not Analyze). Party / Account /
Dimension / Currency / Item Analyze never reached `analyze_row_as_filter()`.

**Secondary bug:** After Party identity API success, `refresh_summary()` always
called `hide_member_panel()` whenever `view_axis !== "unified_party"`, wiping
the Unified Party Analysis panel immediately after paint.

**Why prior Playwright passed:** It called `ae.analyze_party_identity(row)`
programmatically and only asserted button presence — it never clicked the DOM
button through the broken handler.

**Fix:**
1. Route `action === "analyze"` to `analyze_row_as_filter()` (or construction /
   inventory handlers) before voucher actions.
2. Keep the Party identity panel across summary refresh when
   `party_scope.unified_identity` is set.
3. Push Analysis Path breadcrumb, sync filter controls, and refresh the Parties
   grid to the identity member scope after Analyze.
4. Rewrite Playwright to dispatch a real Analyze click, wait for
   `get_party_identity_analysis`, assert panel + scoped grid + path change.

### A. Production backup restore (Development only)
- **Source backup:** `20261027_111248-erp_espadpharmed_com-database.sql.gz` (432 MB, gzip OK, MariaDB dump).
- **Destination:** `development.localhost` only. Production was not modified.
- **Safety backup:** `pre_restore_safety/20261027_082056_dev_before_prod_restore/` (~438 MB gzip verified).
- **Post-restore isolation:** `mute_emails=1`, scheduler disabled, Production email accounts disabled, notifications disabled, Dev `site_config` / secrets preserved.
- **Migrate:** `bench migrate` on Development after restore (compatibility).

### B. Environment versions (Development)
| Component | Version |
|-----------|---------|
| Frappe | 16.52.0 |
| ERPNext | 16.51.1 |
| HRMS | 16.51.0 |
| erpnext_extensions | **5.6.21** |
| persian_calendar | 2.1.0 |

### C. Party Name correction
**Root cause:** Parties axis used Selling/Buying naming settings to decide whether
to look up `customer_name` / `supplier_name`. Production Selling Settings =
`Customer Name` while Customer documents use series IDs (`CUS-00003`), so Party
Name incorrectly showed the internal code.

**Fix:** Party Name always resolves from the master display field
(`customer_name` / `supplier_name` / `employee_name` / `title`), falling back to
Party ID only when the master name is missing.

### D. Identifier Field / Party identity configuration
Configured per Party Type in Iran Accounting Settings (Account Explorer Party Sources).
Identifier Field is **not** hardcoded to `tax_id`.

| Party Type | Identifier Field (Production config) |
|------------|--------------------------------------|
| Customer | `tax_id` |
| Supplier | `tax_id` |
| Employee | `custom_national_code` |
| Shareholder | `custom_national_code` |

Respects: Enabled, Party Type, Identifier Field, Show in Unified Party, Sequence.

Identifier **values** are returned as `party_identifier` (strings; leading zeros
preserved), shown as **Identifier Field**, included in CSV/XLSX export as text,
and resolved in batched lookups (no N+1).

### E. Related Roles
Parties axis adds **Related Roles**:
- For each Party row, find other **enabled** Party Types with the same normalized,
  nonempty Identifier Field value.
- Example (restored snapshot): Customer `CUS-00003` / میلان پارس →
  `Customer, Supplier, Shareholder` (identifier `14008809078`).
- Blank identifiers never match. Names alone never match.
- Balances are **not** auto-merged. Inaccessible Party masters are not exposed.

### F. Party filtering
Account Explorer filter architecture supports:
- Party Type
- Party (document ID)
- Party Name
- Identifier Field
- Unified Party / Related Identity (`unified_identity` + resolved member tuples)

When Unified Identity is selected, related roles for the identity are included.
When filtering by Party Type or exact Party ID alone, scope stays narrow unless
Unified Identity is explicitly enabled. Document-level party filters are skipped
when `resolved_member_tuples` are set so Supplier/Shareholder members are not
excluded under a Customer-originated identity analysis.

### G. Unified Party Analyze
Parties → Analyze → Unified Party Analysis panel:
- Identity summary, related roles, Party IDs, display names, identifier
- Per-role opening/period/closing debit & credit (GL authoritative)
- Combined analytical totals (not a legal settlement / offset)
- Show Related Vouchers (preserves source Party Type + Party ID; no duplicate GL)

**Financial isolation:** Customer and Supplier sharing an identifier remain distinct
accounting parties. No GL Party rewrite, no Party master merge, no receivable/payable netting.

### H. Consistent Analyze navigation
| Axis | Expand/collapse icons | Analyze |
|------|----------------------|---------|
| Account Levels | Removed | Yes (account / subtree scope) |
| Parties | Removed | Yes → Unified Party Analysis |
| Dimensions | Removed | Yes (selected dimension value) |
| Currencies | Removed | Yes (selected currency) |
| Item Groups | Removed | Yes (group + descendants) |
| Items | Removed | Yes (selected item; warehouse preserved) |
| Vouchers | Removed | **No** (Voucher No / Open Voucher only) |

Axis switching, account hierarchy level selection, sort/filter/pagination/selection/
copy/export remain. Analyze preserves Company, Fiscal Year, Date Range, Finance Book,
and compatible filters; incompatible axis filters are not carried blindly.

### I. Standard Parties axis retained
Distinct accounting rows per Party Type (Customer / Supplier / …) with:
Party Type, Party, Party Name, Identifier Field, Related Roles,
Debit/Credit Turnover, Debit/Credit Balance, Analyze.

### J–K. Performance
**Measured on restored snapshot (~766k GL / ~172k SLE):**

| Path | Before | After |
|------|--------|-------|
| Metadata currency enrichment (cold) | ~3030 ms (full GL DISTINCT) | ~21–187 ms (Account master + company default) |
| Account Levels first page (API) | ~2450–2700 ms | ~460–800 ms |
| Parties first page (with Related Roles) | ~36–160 ms | ~180–200 ms (identity batch; still interactive) |
| Voucher first page | ~2.8 s | ~2.8 s |

1. Currency discovery no longer full-scans `tabGL Entry` on every cold open.
2. Covering GL index `ae_gl_co_iscancel_posting_acct`
   `(company, is_cancelled, posting_date, account)` with FORCE INDEX for
   company-wide Account Levels aggregates.
3. Related Roles / identity resolution uses batched master lookups (no per-GL N+1).

Experimental Redis financial caches were **not** activated.

### L. Financial reconciliation
- FY1404 on this snapshot: no GL activity; Account Levels totals remain zero (no FY1405 leakage).
- FY1405 Account Levels leaf rollups match Case B measures.
- Matching tax IDs across Customer/Supplier/Shareholder do **not** merge balances;
  Unified Analyze shows per-role + combined analytical totals only.
- Direct/Prepared parity preserved for voucher paths.

### M. Export
CSV/XLSX (All Filtered / Selected; Jalali / Gregorian) include:
Party Type, Party, Party Name, Identifier Field, Related Roles.
Identifiers exported as text (leading zeros preserved).

### N. Tests / Playwright / Build
- Unit/regression including `test_account_explorer_party_identity_analysis_v5621`
  (customer-only, multi-role, missing/disabled identifiers, filters, isolation,
  no duplicate GL, Analyze presence rules) — **OK** (11 tests).
- Playwright `playwright_account_explorer_party_identity_v5621.mjs` — **OK**
  with **real DOM Analyze click** on CUS-00003:
  - identity API observed from click (~39 ms)
  - visible panel + scoped grid (~280 ms)
  - Analysis Path changed; navigate back restores scope
  - Account Levels Analyze scopes; remount survives; Vouchers have no Analyze
- Currency Analyze click spot-check: handler fires and applies currency filter.
- `bench build --app erpnext_extensions` — **OK**.

### O. Migration requirements
- Run `bench migrate` to apply patch
  `ae_gl_company_range_index_v5621` (creates GL covering index if missing).
- Index creation may take a few seconds on large Production GL tables.
- No GL/SLE data mutations.
- No new DocType required for Related Roles / Unified Analyze (runtime resolution).

### P. Known limitations
- Voucher first page remains ~2.5–3.0 s on this volume (target met; further gains deferred).
- Currency filter list may include Account currencies unused in GL (acceptable for dropdowns).
- Combined analytical totals are informational only — not settlement.
- Rapid DataTable remount can surface a benign Frappe `cssRules` race (non-blocking).

### Q. Deployment checklist
1. Deploy erpnext_extensions **5.6.21**
2. `bench migrate` (GL index patch)
3. `bench build --app erpnext_extensions` / clear assets cache as usual
4. `bench clear-cache`
5. Spot-check Parties: Party Name, Identifier Field, Related Roles, Analyze → identity panel
6. Spot-check Analyze on Account Levels / Dimensions / Currencies / Item Groups / Items
7. Confirm Vouchers has no Analyze; document navigation still works
8. Confirm no expand/collapse icons on any axis
9. Spot-check Account Levels FY1405 first load timing
10. Confirm no Redis prototype flags enabled

### Safety confirmations
- Development restored from the specified Production backup
- Production was not modified
- Version is 5.6.21 (not bumped)
- No GL/SLE source mutations
- No Redis prototype activation
- No push / no deployment from this work package
- Unrelated uncommitted facility/payroll scripts were not included in the commit
