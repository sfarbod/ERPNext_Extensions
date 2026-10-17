# Release 5.6.0 — ERPNext 16.51.1 / Frappe 16.52.0 compatibility

**Version:** 5.6.0  
**Date:** 2026-10-17  
**Previous:** 5.5.53  
**Branch:** `feature/erpnext-16-51-1-compatibility`  
**Validated stack:** Frappe **16.52.0**, ERPNext **16.51.1** (core unmodified)  
**Site:** `development.localhost`  
**Production:** **untouched** — no push, no merge to `develop`

---

## A. Summary

Opens explicit, fingerprint-guarded support for **ERPNext 16.51.1** and **Frappe 16.52.0**,
updates Iran Accounting RIV/UVR wrappers for Core stock-ledger redesigns, and adapts Desk
navigation patches to Frappe’s `Workspace Sidebar` → `Sidebar` / `Custom Sidebar` migration.

Accounting contracts (TYPE A/B/C, manufacture residual, rate-first RIV, Pattern A precision
loss) are preserved. Unsupported ERPNext/Frappe minors remain **blocked**.

---

## B. Version information

| Component | Version |
|-----------|---------|
| erpnext_extensions | **5.6.0** |
| ERPNext | **16.51.1** (`5af6561`) |
| Frappe | **16.52.0** (`4515014`) |
| HRMS | 16.16.0 |

Site `list-apps` after migrate matches package versions above.

---

## C. Compatibility guards (verified)

| Guard | ERPNext allow-list | Frappe allow-list | Notes |
|-------|--------------------|-------------------|-------|
| `riv_rate_guard` | 16.29–16.37, **16.51** | 16.29–16.36, **16.52** | New fingerprints; sabb optional |
| `uvr_regional_guard` | 16.29–16.37, **16.51** | 16.29–16.36, **16.52** | UVR `get_stock_qty` alternate |
| JC component-return patch | 16.36, 16.37, **16.51** | n/a | Method body **unchanged** |
| Pattern A precision-loss | shares RIV lists | shares RIV lists | Fingerprint **unchanged** |

**Still blocked (examples):** ERPNext 16.38–16.50 / 16.52+, Frappe 16.37–16.51 / 16.53+  
(not fingerprint-validated on this bench).

---

## D. Core API changes (ERPNext 16.37 → 16.51.1)

### Verified compatible / adapted

| Module | Core function | Change | Extension action |
|--------|---------------|--------|------------------|
| Stock Ledger | `update_rate_on_stock_entry` | Recalc only when `not sle_id and not dependant`; sabb helper **removed** | Duck-typed `vanilla_recalculates_after_update_rate`; new fingerprint |
| Stock Ledger | `recalculate_amounts_in_stock_entry` | Sets `flags.via_repost = True` | New fingerprint; wrappers call Core as-is |
| Stock Ledger | `is_manufacture_entry_with_sabb` | **Removed** (was briefly `has_bundle_picked_on_submit`) | Optional fingerprint; duck-type gate |
| BuyingController | `update_valuation_rate` | Uses `get_stock_qty` for stock-UOM qty | Alternate fingerprint; regional hook still called |
| BuyingController | `update_regional_item_valuation_rate` | Unchanged stub + `@allow_regional` | Fingerprint match |
| Stock Entry | `validate_work_order_status_for_return` | Unchanged | JC bypass enabled on 16.51 |
| AccountsController | `make_precision_loss_gl_entry` | Unchanged | Pattern A wrap OK |
| Asset | `scrap_asset` / `restore_asset` | Signatures stable vs 16.37 for wrapped path | Existing 5.5.53 wrappers OK |

### Desk / Frappe navigation

| API | Status | Extension action |
|-----|--------|------------------|
| `create_workspace_sidebar_for_workspaces` | **Removed** | Optional import via `desk_navigation` |
| `Workspace Sidebar` | Inert archive (migrate-writable) | Dual-write when writable |
| `Sidebar` / `Custom Sidebar` | Runtime navigation | Petty/Facility → `Sidebar`; Cheque/Guarantee → **site Custom Sidebar on Accounts** (never mutate ERPNext standard Accounts `Sidebar`) |

---

## E. Accounting compatibility

| Area | Status |
|------|--------|
| TYPE A/B/C residual classification | Unchanged |
| Rate-first RIV preserve | Guard + wrapper updated for 16.51 gate; unit/integration preserve suite **PASS** |
| UVR regional Iran binding | Fingerprint updated; suite **PASS** |
| Pattern A FX PI precision-loss skip | **PASS** on 16.51.1 / 16.52.0 |
| Asset Scrap/Restore VAD | Suites **PASS** |
| Manufacture residual / pool asserts | Unchanged code paths |
| Historical valuation transitional guards | Unchanged |

**Documented Core behavioral difference (not silently papered over):**  
ERPNext 16.51 sets `via_repost` inside `recalculate_amounts_in_stock_entry` and always
recalculates Stock Entry economics from `get_incoming_outgoing_rate_from_transaction`.
One atomicity-chain scenario
(`test_corrupted_manufacture_byproduct_vr_restores_atomic_on_riv`) **fails** under that
Core path; see Known limitations.

---

## F. Database migration

| Step | Result |
|------|--------|
| Backup | `20261017_192523-development_localhost-database.sql.gz` (+ files) |
| `bench --site development.localhost migrate` | **Success** (after Desk navigation fix) |
| Installed apps metadata | erpnext 16.51.1, frappe 16.52.0, erpnext_extensions 5.6.0 |

No production site touched. No destructive DB ops.

---

## G. Tests executed

| Suite | Result |
|-------|--------|
| `test_riv_rate_first_preserve` (19+1) | **OK** |
| `test_uvr_regional_guard` (30) | **OK** (1 skipped) |
| `test_fx_pi_pattern_a_precision_loss_guard` (20) | **OK** (6 skipped) |
| `test_scrap_restore_vad_v5553` / `test_scrap_restore_rpc_boundary` | **OK** |
| `test_riv_propagated_rate_v5524` | 14 OK / **1 FAIL** (see below) |
| JC component return v542 | Unit/fingerprint **OK**; 3 live canary fails (data) |

---

## H. Known limitations / untested

1. **ERPNext 16.38–16.50** not allow-listed (fingerprints not measured).  
2. **`test_corrupted_manufacture_byproduct_vr_restores_atomic_on_riv`** — fails on 16.51.1;
   likely interaction with Core `via_repost` / always-recalculate SE path. Needs dedicated
   follow-up before relying on that edge restore in production RIV campaigns.  
3. **JC canary `PO-JOB-10492`** live returnable/report asserts — environment/data drift;
   `test_N35_patch_fingerprint_enabled` **passes**.  
4. Full regression of all iran_accounting / petty / cheque modules not re-run in this cut.  
5. Intermediate Frappe minors 16.37–16.51 not allow-listed (only 16.52 added with 16.36).

---

## I. Breaking changes

None intentional for extension public APIs.  
**Deploy requirement:** Frappe ≥16.52 when using ERPNext 16.51.1 with this release’s guards
(pair validated together). Older Frappe 16.36 + ERPNext 16.37 remain supported by allow-list
when those Core fingerprints match.

---

## J. Deployment instructions

1. Backup site.  
2. Update apps: Frappe → 16.52.0, ERPNext → 16.51.1, erpnext_extensions → 5.6.0.  
3. `bench --site <site> migrate`  
4. Confirm `bench version` / `list-apps` show 16.51.1 / 16.52.0 / 5.6.0.  
5. Confirm Iran monkey patches install (no RIV/UVR fingerprint RuntimeError in Error Log).  
6. Spot-check Petty/Facility module sidebars and Accounts Custom Sidebar cheque links.

---

## K. Rollback instructions

1. Restore DB backup `20261017_192523-…` (or site-specific backup taken before upgrade).  
2. Check out previous erpnext_extensions tag/commit (5.5.53).  
3. Reinstall prior Frappe/ERPNext pair if rolling those back.  
4. `bench --site <site> migrate`  
5. Do **not** keep 5.6.0 guards on an unvalidated Core build.

---

## L. Modified files (high level)

- `erpnext_extensions/__init__.py` — version 5.6.0  
- `iran_accounting/domain/riv_rate_guard.py` — allow-list, fingerprints, duck-typed recalc gate  
- `iran_accounting/domain/uvr_regional_guard.py` — allow-list, UVR fingerprint alternate  
- `iran_accounting/domain/riv_valuation_guard.py` — Iran recalculate sets `flags.via_repost` (16.51 parity)  
- `iran_accounting/domain/irr_residual_classification.py` — UVR stock qty mirrors Core `get_stock_qty`  
- `stock_extensions/job_card_component_return/patch.py` — 16.51 allow  
- `desk_navigation.py` — **new** Sidebar dual-write helpers  
- `cheque_management/payments_sidebar.py` — Custom Sidebar (Accounts), no Core export  
- Petty/Facility workspace patches — import/sync for 16.52  
- Related unit tests + `RELEASE_5_6_0.md`

---

## M. Post-audit follow-up (same release)

Addressed remaining items from the Core API audit ([Audit ERPNext 16.51.1 API diffs](b5010fb4-1f53-4cfa-9bbe-f711d4f49bdb)):

1. Iran `make_recalculate_amounts_wrapper` now sets `via_repost` before `calculate_rate_and_amount`.  
2. Residual classifier stock-qty divisor aligned with Core `get_stock_qty`.  

Still open (non-blocking for guard install):  
`test_corrupted_manufacture_byproduct_vr_restores_atomic_on_riv` under Core’s always-recalculate SE path.
