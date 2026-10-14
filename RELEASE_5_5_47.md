# Release 5.5.47 — Phase 2 Historical Rebuild Campaign

**Version:** 5.5.47  
**Date:** 2026-10-14  
**Previous:** 5.5.46  
**Branch:** `feature/5.5.47-historical-rebuild-campaign`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0**, Iran Accounting **5.5.47**  
**Site:** `development.localhost` (Production DB clone) — **no production deploy**, **no push**, **no tag**

**Backup before mutate:** `20261014_223331-development_localhost-database.sql.gz`

---

## A. Objective

Independent **Historical Rebuild Campaign** that lists **all** submitted stock-affecting documents and orchestrates:

1. Existing Iran source reconstruction (when required)
2. Core **Repost Item Valuation** (authoritative valuation engine)
3. SED / SLE / SABE / Bin / GL regeneration via Core + Iran hooks
4. TVA supersession (`SUPERSEDED_BY_PHASE2`)

Does **not** replace Core RIV algorithms or invent parallel valuation.

---

## B. New DocTypes

| DocType | Role |
|---------|------|
| Historical Rebuild Campaign | Campaign header, counters, cursor, pause/stop flags, logs |
| Historical Rebuild Campaign Item | One row per historical voucher (indexed, unique campaign+voucher) |
| Historical Rebuild Campaign Log | Child execution log rows |

---

## C. Server commands

```bash
bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.create

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.build_manifest \
  --kwargs '{"campaign":"HRC-00001"}'

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.start \
  --kwargs '{"campaign":"HRC-00001"}'

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.pause \
  --kwargs '{"campaign":"HRC-00001"}'

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.stop \
  --kwargs '{"campaign":"HRC-00001"}'

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.resume \
  --kwargs '{"campaign":"HRC-00001"}'

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.status \
  --kwargs '{"campaign":"HRC-00001"}'
```

`start` / `resume` process **continuously** until PAUSE, STOP, or completion. Optional `max_items` for pilot runs.

---

## D. Pause / Stop semantics

- **PAUSE / STOP** stop **new selections** from the campaign list only.
- Already-created Core RIV continue under normal Core behavior.
- Unrelated user/system RIV are never cancelled or blocked by the campaign.
- Under Maintenance Mode, campaign **self-drives** RIV via `execute_reposting_entry` (schedulers inactive).

---

## E. Manifest scope

Stock Entry, Purchase Receipt, Delivery Note, Stock Reconciliation, Subcontracting Receipt, Purchase Invoice (`update_stock=1`), Sales Invoice (`update_stock=1`).

Order: `posting_date, posting_time, creation, doctype, name`.

---

## F. Known limitations

1. Full 15k+ voucher rebuild wall-clock is dominated by Core RIV future-SLE walks; multi-session Pause/Resume is required for production windows.
2. Source reconstruction beyond Stock Entry uses Core RIV only (no invented formulas).
3. TVA Stock Adjustment GL rows are superseded for discovery without automatic GL reverse.
4. Production site must not be mutated from this release without a separate ops plan.

---

## G. Local execution evidence (HRC-00001)

| Metric | Value |
|--------|------:|
| Manifest total | 15,802 |
| Stock Entry | 14,173 |
| Purchase Receipt | 1,343 |
| Delivery Note | 212 |
| Stock Reconciliation | 31 |
| Subcontracting Receipt | 42 |
| Sales Invoice (update_stock) | 1 |
| Completed (pilot + continuous) | growing — see STATUS |
| Skipped Covered | growing as identities covered |
| Core RIV created/completed | 1,400+ during pilot |

Verified locally:

- CREATE / BUILD MANIFEST / START / PAUSE / STOP / RESUME / STATUS
- Maintenance Mode on during START (campaign-driven `execute_reposting_entry`)
- Pause stops new selections only
- Skipped Covered when Completed covering RIV exists
- Transaction RIV for large multi-identity vouchers
- RQ purge required after bulk DocType inserts flooded default queue
- Unit/regression: 80 tests OK (prevention, residual, CC, native transfer, campaign)

**Not yet complete:** full drain of all 15,802 vouchers (multi-hour/day Core RIV wall-clock). Continuous resume left running for operator continuation.

## H. Verdict (local)

**IMPLEMENTED_BUT_REBUILD_INCOMPLETE** — architecture and controls verified; full historical population not fully drained in this session.
