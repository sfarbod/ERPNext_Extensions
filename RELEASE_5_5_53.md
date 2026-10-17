# Release 5.5.53 — Asset Scrap/Restore valuation + JE integrity (hardened)

**Version:** 5.5.53  
**Date:** 2026-10-17  
**Previous:** 5.5.52  
**Branch:** `develop`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (core unmodified)  
**Also reviewed (read-only):** ERPNext **v16.51.1**, upstream develop `a1ff01724d`  
**Site:** `development.localhost`  
**Production:** **untouched** — no push from this task

---

## A. Summary

Repeated Asset **Scrap → Restore** cycles are valuation-neutral and journal-neutral
when cancellation is permitted:

1. Asset header VAD stays aligned with the **default** Finance Book VAD.
2. Scrap-day Depreciation Entries are **cancelled** on Restore (prefer cancel;
   documented reverse fallback only if cancel is blocked by a closed/frozen period).
3. Restore **calls core** `restore_asset` — only the reverse step is replaced —
   preserving core permissions, schedule reset, scrap JE cancel, status, and activity.

Validated on Asset **3455** @ **2026-09-23** × 3 cycles after hardening.

---

## B. Root causes (ERPNext core)

| # | Defect | Core path |
|---|--------|-----------|
| 1 | Asset VAD not restored on Restore | Reverse JE expense **credit** skips `update_asset_on_depreciation`; FB updated only |
| 2 | Orphan reverse JE pairs | `create_reverse_depreciation_entry` (`posting_date=nowdate()`) instead of cancelling scrap-day Depreciation Entry |

Upstream `a1ff01724d` (develop, **not** in v16.51.1) fixes #1 via
`Asset.sync_value_after_depreciation()` but still creates reverse JEs (#2 remains).

---

## C. Fix design (compatibility-first)

**Files**

| File | Role |
|------|------|
| `services/scrap_restore_vad.py` | Cancel-or-reverse unwind + default-FB VAD sync + `restore_asset_accounting` |
| `integration_hooks.py` | Whitelist wrappers for scrap/restore |
| `hooks.py` | `override_whitelisted_methods` (unchanged keys) |

**Restore sequence**

1. Temporarily replace `reverse_depreciation_entry_made_on_disposal` with
   `cancel_or_reverse_disposal_day_depreciation` (same eligibility as core).
2. Call **core** `depreciation.restore_asset` (Asset write permission, schedule reset,
   `cancel_journal_entry_for_scrap`, `set_status`, asset activity).
3. `sync_asset_vad_from_finance_book` from **default** Finance Book row
   (`get_default_finance_book_idx`, mirrors `a1ff01724d`).

**Accounting policy (cancel vs reverse)**

| Situation | Action |
|-----------|--------|
| Cancel permitted | Cancel scrap-day Depreciation Entry (period-correct GL; restores Asset+FB VAD via unlink) |
| Cancel blocked by closed/frozen period | **Documented** fallback to core reverse + msgprint; then VAD sync |
| Permission error on cancel | **Raise** — never `ignore_permissions`, never silent fallback |

No ERPNext core patch. No GL SQL deletes.

---

## D. Hardening vs earlier draft

| Issue | Correction |
|-------|------------|
| Full reimplementation of restore (skipped core) | Now **calls core** `restore_asset` |
| `ignore_permissions` on JE cancel | **Removed** |
| Finance Book row `[0]` always | Default Finance Book semantics |
| Broad `except Exception: pass` on booked count | **Removed** |
| Cancel always, even if period closed | Period-lock → reverse fallback with user-visible message |
| Missing asset | Explicit ValidationError |

---

## E. Historical cleanup (development.localhost only)

| Item | Detail |
|------|--------|
| Backups | `20261017_170945-…`, `20261017_185524-…` |
| Orphan pairs | Prior core-style reverse pairs cancelled; baseline VAD forced |
| Preserved | Monthly depr JEs `166537`…`166542` |
| Unrelated | `ACC-JV-1405-169100` = Asset Disposal for Asset **1003** — untouched |

---

## F. Asset 3455 × 3 cycles @ 2026-09-23 (post-hardening)

| Step | Status | Asset VAD | FB VAD | Accum | Scrap-cycle submitted after restore |
|------|--------|-----------|--------|-------|-------------------------------------|
| Baseline | Partially Depreciated | 108,350,037 | 108,350,037 | 31,649,963 | 0 |
| C1 Scrap | Scrapped | 108,220,432 | 108,220,432 | 31,779,568 | 2 |
| C1 Restore | Partially Depreciated | **108,350,037** | **108,350,037** | **31,649,963** | **0** (cancel) |
| C2 Scrap/Restore | same pattern | baseline | baseline | baseline | **0** |
| C3 Scrap/Restore | same pattern | baseline | baseline | baseline | **0** |

`core_restore_called=true`, `reverse_fallback=[]` on all three restores.  
Monthly JEs `166537`…`166542` remain docstatus=1.

---

## G. Tests

| Suite | Count | Result |
|-------|-------|--------|
| `test_scrap_restore_vad_v5553` | 9 | **PASS** |
| `test_scrap_restore_rpc_boundary` | 5 | **PASS** |
| `test_scrap_restore_hardening_v5553` | 9 | **PASS** |
| Asset 3455 × 3 cycles (site probe) | — | **PASS** |

Hardening coverage: core restore invoked, permission propagation, period-lock
fallback, no permission fallback, Guest denied, default FB sync, JE cleanliness.

Playwright Desk UI: not re-run here (prior locale hang on this site); accounting
correctness proven via live site cycles + JE/GL assertions.

---

## H. Compatibility matrix (core restore vs custom)

| Responsibility | Core | Custom | Status |
|----------------|------|--------|--------|
| Whitelist / fixed signature | yes | yes (wrapper) | Preserved |
| `has_permission(Asset, write)` | yes | via core | Preserved |
| Reverse disposal-day depr | reverse JE | cancel-first (+ fallback) | **Intentional change** |
| Reset depreciation schedule | yes | via core | Preserved |
| Cancel Asset Disposal scrap JE | yes | via core | Preserved |
| `set_status` / activity | yes | via core | Preserved |
| Asset header VAD sync | no (bug) | yes (default FB) | **Added** |
| `ignore_permissions` on cancel | no | no | Preserved |
| Status must be Scrapped | not enforced | not enforced | Same as core |

v16.51.1 `restore_asset` signature and body match v16.37.0 for this path.
Future `a1ff01724d` sync is compatible (absolute assign; dual sync is idempotent).

---

## I. Migration / remaining risks

- No global data patch. Historical reverse pairs need controlled cleanup per site.
- Native ERPNext Restore (bypassing whitelist override) still creates reverse JEs.
- Cancel requires open period for scrap posting date; otherwise reverse fallback.
- Partial failure mid-restore has the same atomicity limits as core (no mid-step commits added).

---

## J. Confirmations

- **VERSION:** 5.5.53  
- **PRODUCTION TOUCHED:** NO  
- **ERPNext CORE MODIFIED:** NO  
- **PUSHED:** NO  
