# Release 5.5.42 — PM Clearance Cancelled PI Return + Purchase Invoice Cancel Guard

**Version:** 5.5.42  
**Date:** 2026-10-13  
**Previous:** 5.5.41  
**Branch:** `develop`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (unmodified Core)

**Validation scope:** Development site recovery + unit/integration + Playwright Desk E2E.  
**Production:** Not deployed by this release preparation. No automatic mutation of historical documents.

---

## A. Summary

1. **Development site recovery** — root cause was `maintenance_mode=1` after restore → `frappe.SessionStopped`; secondary `BrokenPipeError` came from printing the traceback to a closed stdout pipe.
2. **PM Return for Correction** now succeeds when a linked Purchase Invoice is already cancelled (legacy / race leftover), scoped via `frappe.flags.pm_return_for_correction`.
3. **Server-side Purchase Invoice `before_cancel` guard** blocks cancel while any **active** PM Clearance references the PI (including Draft).
4. **Concurrency** — named locks + InnoDB `FOR UPDATE` locking reads so cancel cannot miss a just-committed Clearance under REPEATABLE READ.
5. **Legacy diagnostic** — report-only listing of active Clearances that still reference cancelled PIs (includes target CLR).

---

## B. Development Site Failure

| Item | Finding |
|------|---------|
| Original exception | `frappe.SessionStopped("Session Stopped")` from `maintenance_mode` |
| Secondary | `BrokenPipeError` while `print(frappe.get_traceback())` wrote to a closed pipe |
| Fix | `bench --site development.localhost set-maintenance-mode off` + restart web |
| Verification | HTTP 200 login/desk; Playwright Scenario A; target CLR opens |

---

## C. Target PM Clearance

| Field | Value |
|-------|-------|
| Name | `CLR-HR-EMP-0024-202609-b39de9014e16` |
| Workflow | Pending Finance Review |
| Status | Pending Approval |
| Cancelled PI | `ACC-PINV-2026-01370` (row 9, docstatus 2) |
| Pre-fix Return error | `CancelledLinkError: Cannot link cancelled document: Row #9: Purchase Invoice: ACC-PINV-2026-01370` |

---

## D. Root Cause (Return)

1. Frappe core `_validate_links()` runs **before** `before_validate`/`validate` and throws `CancelledLinkError` on cancelled PI links.
2. `validate_purchase_invoices_for_prepare` also threw on `docstatus=2` even when `pm_return_for_correction` skipped only over-allocation.

Ordering: workflow Return → save → `_validate_links` (fail) → never reached readiness skip.

---

## E. Return Fix (scoped)

| Change | Scope |
|--------|-------|
| `PMClearance._validate_links()` narrow exception: during `pm_return_for_correction`, still reject **missing/invalid** Links; allow **cancelled** only when `fieldname == purchase_invoice` | Return path only |
| `validate_purchase_invoices_for_prepare` skips cancelled throw when flag set | Return path only |
| Finance Approve / settlement / normal Draft save | Still block cancelled PI |

**Safety audit (post-c49aa47):** Frappe `flags.ignore_links` skips *all* of `_validate_links` (missing + cancelled, parent + child, Link + Dynamic Link). A Return-scoped `ignore_links=True` therefore allowed nonexistent Project during Return. Replaced with the narrow override above. Does **not** use `ignore_links` on the Return path. Does **not** reactivate PI, remove rows, or bypass workflow/permissions.

---

## F. Purchase Invoice Cancellation Guard

| Item | Detail |
|------|--------|
| Hook | `Purchase Invoice` → `before_cancel` → `purchase_invoice_clearance_guard.before_cancel_purchase_invoice` |
| Active | `docstatus < 2` AND `status NOT IN ('Cancelled','Rejected')` (same as funding reservation) |
| Excluded | Cancelled, Rejected, Deleted |
| Error | Lists readable active Clearance names; does not leak unauthorized names |
| Release | Remove/replace PI on Clearance, then cancel |

---

## G. Concurrency Model

- MariaDB named locks `pm_pi_clr:<PI>` in sorted name order (held until commit/rollback).
- InnoDB `SELECT … FOR UPDATE` on PI rows during Clearance validate and PI cancel.
- Cancel path uses **locking** Clearance lookup (`FOR UPDATE`) to avoid REPEATABLE READ snapshot hiding a just-committed Clearance.
- Deterministic lock order: sorted PI names.
- Test 25: concurrent cancel vs Clearance create — no committed (cancelled PI + active Clearance) state.

Residual risk: extreme isolation edge cases across non-standard cancellation helpers that bypass `before_cancel` are outside supported paths.

---

## H. Legacy Data

- Diagnostic: `report_legacy_cancelled_pi_on_clearances()` (no mutation).
- Target CLR appears in the report.
- Corrective path: Return → Draft → remove/replace cancelled PI → resubmit.

---

## I. Files Changed

**Production**

- `petty_management/doctype/pm_clearance/pm_clearance.py` — narrow `_validate_links` (cancelled PI only on Return)
- `petty_management/services/purchase_invoice_readiness.py` — skip cancelled on Return flag
- `petty_management/services/clearance_service.py` — early PI lock/cancelled check
- `petty_management/services/purchase_invoice_clearance_guard.py` — **new**
- `hooks.py` — PI `before_cancel` hook

**Tests / Playwright / Release**

- `tests/test_pm_clearance_cancelled_pi_return_v5542.py`
- `tests/test_pm_clearance_link_validation_safety_v5542.py` — negative Link matrix A–O
- `e2e/pm_clearance_cancelled_pi_v5542_prep.py`
- `e2e/playwright_pm_clearance_cancelled_pi_v5542.mjs`
- `RELEASE_5_5_42.md`
- `__init__.py` → 5.5.42

---

## J. Unit / Integration Tests (v5.5.42 module)

All **24 PASS** (including Tests 1–25 coverage and legacy diagnostic).  
Link validation safety module: **17 PASS** (A–O + missing Project + no `ignore_links` assignment).

---

## K. Playwright Results

| Scenario | Result |
|----------|--------|
| A Development site | PASS |
| B Target-class Return (Desk Actions) | PASS |
| C PI cancel blocked (Draft Clearance) | PASS |
| D Remove reference releases PM guard | PASS |
| E Full correction flow to Approved | PASS |
| F Unauthorized Return blocked | PASS |
| G Nonexistent Project Link blocked on Return | PASS |

Note: Playwright contexts must use `locale: "en-US"` — default `en-US@posix` breaks Desk Actions via `Intl.Locale`.

---

## L. Regression Results

| Suite | Result |
|-------|--------|
| v5.1.4 outstanding Return | PASS (10) |
| v5.2.11 funding availability Return | PASS (7) |
| v5.5.5 active reservation | PASS (18) |
| v5.5.42 cancelled PI module | PASS (24) |
| v5.5.42 link validation safety A–O | PASS (17) |
| v5.0.4 / v5.0.6 site suites | Flaky on polluted development fixtures (allocation/reservation exhaustion); dedicated v5.5.42 repeated-Return + remark-cancelled tests PASS |

---

## M. Security / Accounting Audit

- No `ignore_permissions` in production fix paths.
- No global accounting bypass.
- No ERPNext/Frappe core modifications.
- Workflow authorization unchanged.
- Finance Approve still blocks cancelled PI.
- Financial history not auto-mutated.
- **Link validation:** Return does **not** set `ignore_links`. Narrow `_validate_links` keeps existence checks for all Links/Dynamic Links; cancelled exception limited to `purchase_invoice`.
- `pm_return_for_correction` cleared in `workflow_hooks` `finally` (no flag leak after ValidationError / CancelledLinkError / unexpected exceptions).

---

## N. Version

**5.5.42**

---

## O / P / Q. Commit / Git / Push

See final report after commit. **NOT PUSHED.**
