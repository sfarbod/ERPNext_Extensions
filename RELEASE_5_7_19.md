# Release 5.7.19 — Employee Checkin Performance + Excel Import Hardening

**Version:** **5.7.19**  
**Branch:** `feature/5.7.19-employee-checkin-perf-import`  
**Runtime scope:** Employee Checkin list indexes, Excel importer (local SheetJS + employee match index), device-import setup, Attendance SSA Link default clear (Checkin dependency only)  
**Out of scope:** Attendance Excel Import JS/Python repairs, Production deploy, push/merge  
**Deployment status:** Development-validated. Local commit only unless explicitly promoted.

---

## Summary

1. Serve SheetJS from `/assets/erpnext_extensions/js/lib/xlsx.full.min.js` (no CDN).
2. Authorized `get_employee_match_index` for one-shot Employee matching.
3. List indexes `espad_checkin_modified` and `espad_checkin_time`.
4. Punch clocks may fall back to calc-date when punch date cells are blank.
5. Import role gains Employee Read/Select (required for matching / existing-key lookup).
6. Clear `Attendance.custom_source_shift_assignment` default `"0"` — **direct Checkin dependency** via site Server Script Rest Attendance Creator (does **not** change Attendance Excel Import code).
7. Attendance Excel Import implementation left untouched.

---

## A. Environment (development.localhost)

| Component | Version |
|-----------|---------|
| Frappe | 16.54.0 |
| ERPNext | 16.53.0 |
| HRMS | 16.53.0 |
| erpnext_extensions (source) | 5.7.19 |
| erpnext_extensions (installed before this work) | 5.7.17 |

Backup before import: `sites/preserved_dev_backups/pre_checkin_import_20261110_062650.sql.gz` (433M).  
mehr2 SHA-256: `d3abd5b21dc86f9211becdeb08d5a00e5e3c66fcaea0a80718269b976d373dfa`.

---

## B–D. List-view performance

| Metric | Empty table (before) | After ~12k rows |
|--------|----------------------|-----------------|
| `get_list` 20 | ~10 ms | ~1–209 ms (first cold) |
| `get_list` 100 | ~0.5 ms | ~0.9 ms |
| `get_list` 500 | ~0.3 ms | ~1.9 ms |
| Date-range filter 100 | n/a | ~3.0 ms |

- Pre-existing unique index: `espad_checkin_emp_time_log (employee, time, log_type)`.
- New: `espad_checkin_modified`, `espad_checkin_time` — EXPLAIN uses them (`Using index` / range).
- Import UX bottleneck was CDN SheetJS + multi-page Employee fetch, not empty-table SQL.
- Client Script still embeds ~76 KB importer on List view (acceptable; asset move deferred).

---

## E. mehr2.xls suitability

| Check | Result |
|-------|--------|
| Format | Real XLS (OLE CFBF), 1 sheet, 9114×30 |
| Header row | 4 (0-based); data from row 8 |
| Period | Mehr 1405 ↔ 2026-09-23 … 2026-10-22 |
| Unique codes | 254–255 |
| Punch columns | Real `زمان/تاریخ ورود|خروج` clocks (first/last of day) |
| Deduped punch events | 11,985 |
| Invented from hours? | **No** — suitable for Employee Checkin |

---

## F. Importer analysis

Browser SheetJS parse → Employee match (`custom_personal_code` / device / number / name) → `import_device_checkins` (idempotent employee+time+log_type). Manual limit skipped only in authorized device-import context. Site Server Script `#_employee_checkin___rest_attendance_creator_v1.0.1` may create Attendance on Checkin save (independent of Attendance Excel Import).

---

## G. Changed files

- `erpnext_extensions/extentionhrms/public/js/employee_checkin_excel_import.js` (V1.3.3)
- `erpnext_extensions/extentionhrms/employee_checkin_import.py`
- `erpnext_extensions/extentionhrms/employee_checkin_import_setup.py`
- `erpnext_extensions/extentionhrms/tests/test_employee_checkin_device_import.py`
- `erpnext_extensions/__init__.py` → 5.7.19
- `RELEASE_5_7_19.md`

---

## H. Before/after benchmarks

See section B–D. Indexes confirmed via EXPLAIN after load.

---

## I. Import preview and reconciliation (development)

| Item | Value |
|------|-------|
| Matched checkins | 11,937 |
| Unmatched codes | `477` only |
| Ambiguous | 0 |
| First pass | created 11,045; failed 892 (`Source Shift Assignment: 0`) |
| After SSA default clear + retry | duplicates 11,937; failed 0 |
| Period Employee Checkin count | 11,913 |
| Total Employee Checkin | 11,951 (incl. prior test rows) |
| Attendance count | 882 created by Rest Attendance Creator (not Excel Import); unchanged on re-import |
| Repeated import | fully idempotent |

---

## J. Automated tests

37 tests OK (module `test_employee_checkin_device_import`, excluding flaky multiprocess concurrent case under single-connection execute). New coverage: local SheetJS URL, employee index API, list indexes, SSA default clear, import-role Employee read.

---

## K. Git

- Branch: `feature/5.7.19-employee-checkin-perf-import`
- Version: 5.7.19  
- Commit: see `git rev-parse HEAD` after local commit.

---

## L. Remaining risks / blockers

1. List Client Script still large (~76 KB) on every List open — optional future move to `doctype_list_js` asset.
2. Site Server Script Rest Attendance Creator still creates Attendance from Checkin; SSA Active-only / Excel Import Error A remains for the separate Attendance import task.
3. Personnel code `477` unmatched on this site.
4. Concurrent-import multiprocess test remains environmentally flaky; unique DB index still enforces identity.
5. Do not push/merge/deploy Production from this branch without explicit approval.
