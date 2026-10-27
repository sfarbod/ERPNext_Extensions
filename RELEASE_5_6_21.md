# erpnext_extensions 5.6.21

## Account Explorer — Party Identity, Parties UX, Performance

### Summary
Restores a Production database snapshot into Development, corrects Party Name /
Identifier Field display on the Parties axis, removes unnecessary Parties
drill-down controls, and optimizes Account Explorer first-load performance on
large GL volumes.

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

### D. Identifier Field integration
Configured per Party Type in Iran Accounting Settings (Account Explorer Party Sources):

| Party Type | Identifier Field (Production config) |
|------------|--------------------------------------|
| Customer | `tax_id` |
| Supplier | `tax_id` |
| Employee | `custom_national_code` |
| Shareholder | `custom_national_code` |

Identifier **values** are returned as `party_identifier`, shown in the Parties
grid as **Identifier Field**, included in CSV/XLSX export as text, and resolved
in batched lookups (no N+1). Missing configured fields yield empty values +
controlled warnings (no crash).

### E. Unified Party compatibility
- `show_in_unified_party` remains `0` for all sources on this snapshot.
- Standard Parties axis keeps distinct GL identities (`Customer/CUS-…` vs
  `Supplier/SUP-…`) even when identifiers match.
- No automatic cross-type balance merge. Existing Unified Party registry
  behavior is unchanged.

### F. Parties UI simplification
Applied **only** to the Parties axis:
- No expand/collapse / drill icon
- No Analyze action
- No empty Actions column
- `drill_down_enabled = 0` on party rows

Other axes retain Analyze / drill behavior.

### G–I. Performance root cause and optimization
**Measured bottlenecks on restored snapshot (~766k GL / ~172k SLE):**

| Path | Before | After |
|------|--------|-------|
| Metadata currency enrichment (cold) | ~3030 ms (full GL DISTINCT) | ~21–187 ms (Account master + company default) |
| Account Levels first page (API) | ~2450–2700 ms | ~460–700 ms |
| Parties first page | ~36–160 ms | ~33–76 ms |
| Voucher first page | ~2.8 s | ~2.8 s (unchanged; under 3 s target) |

1. **Currency discovery** no longer full-scans `tabGL Entry` on every cold open.
2. **Covering GL index** `ae_gl_co_iscancel_posting_acct`
   `(company, is_cancelled, posting_date, account)` with FORCE INDEX for
   company-wide Account Levels aggregates (~2700 ms → ~420 ms SQL).

Experimental Redis financial caches were **not** activated.

### J. Financial reconciliation
- FY1404 on this snapshot: no GL activity; Account Levels totals remain zero (no FY1405 leakage).
- FY1405 Account `11100409` hierarchy balances match rolled-up Case B leaf measures (no hardcoded prior baseline).
- Matching tax IDs across Customer/Supplier/Shareholder do **not** merge balances.

### K. Export regression
- Parties export columns include Identifier Field.
- Identifier / party codes coerced to text to preserve leading zeros.

### L–N. Tests / Playwright / Build
- Unit/regression: party identity, currency discovery, party batch enrichment,
  party resolver, FY isolation, selection export, voucher GL lines — **OK**.
- Playwright `playwright_account_explorer_party_identity_v5621.mjs` — **OK**
  (Party Name, Identifier, no Analyze/drill on Parties).
- `bench build --app erpnext_extensions` — **OK**.

### O. Migration requirements
- Run `bench migrate` to apply patch
  `ae_gl_company_range_index_v5621` (creates GL covering index if missing).
- Index creation may take a few seconds on large Production GL tables.
- No GL/SLE data mutations.

### P. Known limitations
- Voucher first page remains ~2.5–3.0 s on this volume (target met; further gains deferred).
- Currency filter list may include Account currencies unused in GL (acceptable for dropdowns).
- Unified Party remains configuration-gated (`show_in_unified_party=0` here).

### Q. Deployment checklist
1. Deploy erpnext_extensions **5.6.21**
2. `bench migrate` (GL index patch)
3. `bench build --app erpnext_extensions` / clear assets cache as usual
4. `bench clear-cache`
5. Spot-check Parties: Party Name + Identifier Field; no Analyze/expand
6. Spot-check Account Levels FY1405 first load timing
7. Confirm no Redis prototype flags enabled

### Safety confirmations
- Development restored from the specified Production backup
- Production was not modified
- Version is 5.6.21
- No GL/SLE source mutations
- No Redis prototype activation
- No push / no deployment from this work package
