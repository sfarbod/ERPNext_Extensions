# Release 5.5.53 — Asset Scrap/Restore valuation integrity

**Version:** 5.5.53  
**Date:** 2026-10-17  
**Previous:** 5.5.52  
**Branch:** `develop`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (core unmodified)  
**Site:** `development.localhost`  
**Production:** **untouched** — no push from this task

---

## A. Summary

Repeated Asset **Scrap → Restore** cycles no longer progressively reduce Asset-level
net book value. After each Restore, Asset `value_after_depreciation` is re-synced to
the Finance Book VAD that core already restores.

Validated on Asset **3455** (posting date **2026-09-23** / 1405-07-01) across three
full cycles: baseline NBV restored every time; disposal-day depreciation amount stable.

---

## B. Root cause

| Item | Detail |
|------|--------|
| Component | **ERPNext core** restore path + **Asset Usage Depreciation** whitelist wrappers |
| Iran Accounting | **Not involved** (manufacture scrap costing only; Asset 3455 has no usage periods) |
| Core function | `erpnext.assets.doctype.asset.depreciation.update_value_after_depreciation_on_asset_restore` |
| Related JE path | `Journal Entry.update_asset_on_depreciation` (submit) / reverse JE has expense **credit** |

**Mechanism**

1. Scrap posts a pro-rata disposal-day Depreciation Entry.
2. JE submit reduces **both** Asset VAD and Finance Book VAD.
3. Restore creates a reverse JE and adds the amount back to **Finance Book VAD only**.
4. Reverse JE does not have expense debit, so Asset VAD is never increased.
5. Next Scrap/Restore cycle widens the Asset↔FB divergence → progressive NBV leakage.

**Evidence on Asset 3455 (before fix)**

| Field | Value |
|-------|-------|
| Expected baseline NBV | 108,350,037 |
| Asset VAD while Scrapped | 108,220,122 |
| FB VAD while Scrapped | 108,609,247 (diverged) |
| Booked count | 8 (inflated by unreversed scrap-day postings) |

---

## C. Fix

**File:** `asset_usage_depreciation/services/scrap_restore_vad.py` (new)  
**Wrappers:** `integration_hooks.scrap_asset` / `restore_asset`

After core Scrap/Restore:

```text
sync_asset_vad_from_finance_book(asset_name)
```

Sets `Asset.value_after_depreciation = Finance Book.value_after_depreciation`.

No ERPNext core patch. No GL SQL deletes. No session user swap.

---

## D. Historical cleanup (development.localhost only)

Authorized cleanup of Asset 3455 scrap/restore test documents via cancel APIs:

| Document | Action |
|----------|--------|
| ACC-JV-1405-169031 (Asset Disposal) | Cancelled |
| ACC-JV-1405-169030 (reverse of scrap-day depr) | Cancelled |
| ACC-JV-1405-168855 (scrap-day depr) | Cancelled |
| ACC-JV-1405-168856 (prior scrap, already cancelled) | Left |

Preserved legitimate monthly depreciation JEs ACC-JV-1405-166537…166542.  
Rescheduled ADS; baseline VAD = **108,350,037**.

Backup: `sites/development.localhost/private/backups/20261017_163717-development_localhost-database.sql.gz`

---

## E. Reproduction after fix (Asset 3455)

| Operation | Status | Asset VAD | FB VAD | Scrap-day depr | Result |
|-----------|--------|-----------|--------|----------------|--------|
| Baseline | Partially Depreciated | 108,350,037 | 108,350,037 | — | OK |
| Cycle1 Scrap | Scrapped | 108,220,432 | 108,220,432 | 129,605.31 | Parity |
| Cycle1 Restore | Partially Depreciated | 108,350,037 | 108,350,037 | — | = baseline |
| Cycle2 Scrap | Scrapped | 108,220,432 | 108,220,432 | 129,605.31 | Stable |
| Cycle2 Restore | Partially Depreciated | 108,350,037 | 108,350,037 | — | = baseline |
| Cycle3 Scrap | Scrapped | 108,220,432 | 108,220,432 | 129,605.31 | Stable |
| Cycle3 Restore | Partially Depreciated | 108,350,037 | 108,350,037 | — | = baseline |

---

## F. Tests

| Suite | Result |
|-------|--------|
| `test_scrap_restore_vad_v5553` (7) | **PASS** |
| `test_scrap_restore_rpc_boundary` (5) | **PASS** |
| Asset 3455 × 3 cycles | **PASS** (`ok: true`) |

---

## G. Migration

No data patch required for production deploy of the code fix.  
Sites with already-diverged Asset vs FB VAD after partial restores need a controlled
one-off sync (same helper) — not auto-applied globally.

---

## H. Remaining risks

- Core still allows Restore when status is not Scrapped (idempotent no-op for scrap JE).
- Upstream core should eventually restore Asset VAD in
  `update_value_after_depreciation_on_asset_restore`; this release compensates in
  `erpnext_extensions` only.
- Partial restores that leave an existing reverse JE can still block a second reverse
  until the reverse is cancelled (documented cleanup path).

---

## I. Confirmations

- **VERSION:** 5.5.53  
- **PRODUCTION TOUCHED:** NO  
- **ERPNext CORE MODIFIED:** NO  
- **PUSHED:** NO  
