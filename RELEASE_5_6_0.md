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
| Rate-first RIV preserve | Guard + wrapper updated for 16.51 gate; suite **PASS** |
| UVR regional Iran binding | Fingerprint updated; suite **PASS** |
| Pattern A FX PI precision-loss skip | **PASS** on 16.51.1 / 16.52.0 |
| Asset Scrap/Restore VAD | Suites **PASS** |
| Manufacture residual / pool asserts | Suites **PASS** |
| Historical valuation transitional guards | Suites **PASS** |
| RIV FIX B2 atomic restore (by-product@VR) | **PASS** after post-restore sync ordering fix |

### RIV atomicity (FIX B2) — 16.51.1 findings

**Contract:** if RIV-propagated Manufacture/Repack economics fail I2/I5, SE valuation
state must match the pre-operation snapshot (no partial poison). Exceptions are not
suppressed into a successful persist; item-scoped RIV soft-skips with deferred recording.

**Root cause under Core 16.51.1:**

1. Core `update_rate_on_stock_entry` no longer recalculates when `sle_id` is set; Iran
   runs recalculate explicitly. `via_repost` can zero FG while By-Product@VR still
   exceeds the pool → I5.
2. Atomic `update_rate` correctly restored SE to the pre-propagation snapshot and set
   `iran_riv_force_sle_restore`.
3. **Bug:** `process_sle` then ran `sync_irr_sle_from_stock_entry_row` *before* consuming
   that flag. At consume-to-zero, `apply_zero_qty_movement_contract` →
   `align_sed_from_native_outgoing_sle` rewrote SED from the rejected SLE rates
   (e.g. consume `basic_rate` 40M → 1e6), undoing the SE restore. A later
   `get_incoming_outgoing_rate_from_transaction` recalculate then snapshotted the
   already-poisoned state.

**Fix (erpnext_extensions only):**

1. Consume `iran_riv_force_sle_restore` and restore SLE/`wh_data` **immediately after**
   vanilla `process_sle`, **before** any Iran SE/SLE sync.
2. When Iran recalculate soft-fails (deferred I2/I5) inside
   `apply_riv_rate_and_recalculate_atomically`, roll back to the *outer*
   pre-propagation snapshot and skip `persist_irr_contract_after_recalculate`.
3. Recalculate persist gate: snapshot → mutate → hard I2/I5 → restore-without-persist
   on failure (no SE poison write).

**Integrity:** guards unchanged; Core unmodified; exception path still records
`INVALID_PROPAGATED_ECONOMICS` and does not persist rejected economics.

---

## F. Database migration

| Step | Result |
|------|--------|
| Backup | `20261017_192523-development_localhost-database.sql.gz` (+ files) |
| `bench --site development.localhost migrate` | **Success** (after Desk navigation fix) |
| Installed apps metadata | erpnext 16.51.1, frappe 16.52.0, erpnext_extensions 5.6.0 |

No production site touched. No destructive DB ops. No production/historical accounting
data modified during JC canary investigation.

---

## G. Tests executed

| Suite | Result |
|-------|--------|
| `test_riv_propagated_rate_v5524` (15) | **OK** (atomicity canary **PASS**) |
| `test_riv_valuation_integrity` (33) | **OK** |
| `test_riv_rate_first_preserve` (20) | **OK** |
| `test_riv_valuation_scope` (17) | **OK** |
| `test_uvr_regional_guard` (30) | **OK** (1 skipped) |
| `test_fx_pi_pattern_a_precision_loss_guard` (20) | **OK** (6 skipped) |
| `test_manufacture_residual_submit_v5330` (14) | **OK** |
| `test_historical_residual_v5551` (13) | **OK** |
| `test_i4_zero_qty_terminal_residual` (6) | **OK** |
| `test_zero_qty_stock_value_v5550` (15) | **OK** |
| `test_i4_material_transfer_lifecycle` (10) | **OK** |
| `test_equal_rate_byproduct_v5336` (7) | **OK** |
| `test_stock_entry_capitalization_amount` (9) | **OK** |
| `test_stock_entry_transfer_gl` (3) | **OK** |
| `test_repost_accounting_ledger_compat` (12) | **OK** |
| `test_multi_fg_riv_wrapper_v5529` (4) | **OK** |
| `test_acquisition_incoming_rate_v5526` (8) | **OK** |
| Asset scrap/restore VAD + RPC boundary | **OK** |
| JC component return v542 unit/fingerprint | **OK** (`N35` enabled) |
| JC live canaries (PO-JOB10492) | **3 FAIL — historical data** (see §H) |
| `test_stage_equivalent_byproduct_bridge_v5335` | 38 OK / **2 FAIL** (~372 IRR on ~2.27e9 canary matrix; pre-existing allocation rounding, not FIX B2) |

---

## H. Known limitations / untested

1. **ERPNext 16.38–16.50** not allow-listed (fingerprints not measured).  
2. **JC live canaries on `PO-JOB10492` / `MFG-WO-2026-00837`** — classified **historical data /
   environment drift**, not Core 16.51 regression and not an extension return-patch
   regression (`test_N35_patch_fingerprint_enabled` **PASS**). Evidence:

   | Test | Expected | Actual | Classification |
   |------|----------|--------|----------------|
   | `test_live_returnable_report` | item `18000001` `returnable > 0` | `issued=100, consumed=100, returnable=0, still_in_wip=0` | Manufacture `MAT-STE-2026-40453` fully consumed WIP; nothing left to return |
   | `test_N24_canary_validate_bypass_savepoint` | same returnable > 0 | same zero returnable map | Same historical consume state |
   | `test_N31_N32_secondary_unchanged` | draft readiness `classified` includes `COMPONENT_SCRAP` | only `MAIN_FG` qty 3858 (`manufactured_qty already covers for_quantity`) | Post-manufacture simulation path; scrap exists on SE evidence / JC secondary but not re-emitted into draft classified map |

   No production or historical accounting rows were modified to force these green.  
3. **`test_stage_equivalent_byproduct_bridge_v5335`** — 2 matrix asserts off by **372 IRR**
   on ~2.27e9 pool (`test_b02_b03`, `test_b18`). Equal-rate by-product suite **PASS**.
   Track separately from FIX B2.  
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
- `iran_accounting/domain/riv_rate_guard.py` — allow-list, fingerprints, duck-typed recalc gate; atomic outer restore when recalculate defers  
- `iran_accounting/domain/riv_valuation_guard.py` — `via_repost` + hard I2/I5 persist gate (restore-without-persist)  
- `iran_accounting/integration/monkey_patches.py` — FIX B2: force-SLE restore **before** Iran sync  
- `iran_accounting/domain/uvr_regional_guard.py` — allow-list, UVR fingerprint alternate  
- `iran_accounting/domain/irr_residual_classification.py` — UVR stock qty mirrors Core `get_stock_qty`  
- `iran_accounting/scrap_costing.py` — `getattr(doc, "purpose")` for non-Document fixtures  
- `stock_extensions/job_card_component_return/patch.py` — 16.51 allow  
- `desk_navigation.py` — **new** Sidebar dual-write helpers  
- `cheque_management/payments_sidebar.py` — Custom Sidebar (Accounts), no Core export  
- Petty/Facility workspace patches — import/sync for 16.52  
- Related unit tests + `RELEASE_5_6_0.md`

---

## M. Post-audit follow-up (same release)

Addressed remaining items from the Core API audit and the RIV atomicity canary:

1. Iran `make_recalculate_amounts_wrapper` sets `via_repost` before `calculate_rate_and_amount`.  
2. Residual classifier stock-qty divisor aligned with Core `get_stock_qty`.  
3. **FIX B2 atomicity closed:** force-SLE restore before sync; outer SE restore when
   recalculate defers; `test_corrupted_manufacture_byproduct_vr_restores_atomic_on_riv` **PASS**.

Remaining non-blocking: JC live-data canaries (§H.2); stage-equivalent by-product matrix
372 IRR drift (§H.3).
