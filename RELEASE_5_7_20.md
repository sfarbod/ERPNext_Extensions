# Release 5.7.20 — Dual Import Fix (Employee Checkin + Attendance Excel)

**Version:** **5.7.20**  
**Branch:** `feature/5.7.19-employee-checkin-perf-import` (continues from 5.7.19 / `7ef14c1`)  
**Scope:** Historical employment eligibility for Checkin import; Rest Attendance Creator suppression during device import; Attendance Excel Import historical Shift Assignment + SSA Link repair  
**Site:** development.localhost only  
**Denylist:** No Production push/merge/deploy

---

## Execution order (authoritative)

1. **Employee Checkin Excel Import** creates punch events (`skip_auto_attendance=1`).
2. During device import, **Rest Attendance Creator** is temporarily disabled (outermost `device_import_context`) so Checkin save does not submit Absent Attendance.
3. Shift Resolver / Attendance Mapper Before-Save scripts still run (do not create Attendance).
4. **Attendance Excel Import** creates/updates daily Attendance from the Excel workbook (historical Inactive SA supported; SSA default empty).
5. HRMS Auto Attendance remains skipped via `skip_auto_attendance` on imported checkins.
6. ESPAD daily resolver is out of band for this import path (scheduler disabled on development).

---

## Root causes fixed

| Issue | Fix |
|-------|-----|
| Left employee `000477` / `HR-EMP-0206` rejected by current status | Employment window: joining ≤ punch date < relieving |
| RAC submitted 872 Absent despite IN/OUT | Suppress RAC Server Script during device import only |
| `custom_source_shift_assignment` default `"0"` | Clear default + repair stored `"0"` links |
| Attendance Error A (Active-only SA) | Preload / resolve submitted SAs including Inactive, by date coverage |

---

## Changed files

- `extentionhrms/employee_checkin_manual_limit.py`
- `extentionhrms/employee_checkin_import.py`
- `extentionhrms/public/js/employee_checkin_excel_import.js` (V1.3.4)
- `extentionhrms/attendance_excel_import.py`
- `extentionhrms/attendance_excel_import_setup.py`
- `extentionhrms/public/js/attendance_excel_import.js` (V2.0.11)
- `extentionhrms/tests/test_employee_checkin_device_import.py`
- `extentionhrms/tests/test_attendance_excel_import.py`
- `__init__.py` → 5.7.20
- `RELEASE_5_7_20.md`

Preserves 5.7.19 performance work (local SheetJS, employee index API, list indexes).

---

## Backup

`sites/preserved_dev_backups/pre_dual_import_fix_20261110_081516.sql.gz` (verified `gzip -t`).

mehr2 SHA-256: `d3abd5b21dc86f9211becdeb08d5a00e5e3c66fcaea0a80718269b976d373dfa`

---

## Development re-import reconciliation

| Metric | Result |
|--------|--------|
| Prior import deletion | Tables already empty (0 / 0); nothing deleted |
| Source punch events imported | **11,985** created, **0** failed |
| Idempotent Checkin re-run | **11,985** duplicates, **0** created |
| Attendance after Checkin (RAC) | **0** (suppressed) |
| HR-EMP-0206 (code 000477, Left) checkins | **48** |
| Unmatched codes | **518** only (30 day rows) — employee does not exist |
| Attendance created | **7,599** (Present **6,402**, Absent **1,197**) |
| Attendance failed / SSA `0` / duplicates | **0 / 0 / 0** |
| Absent with full IN+OUT checkins | **0** |

Tests: **79 OK** (Checkin + Attendance import modules; concurrent multiprocess case excluded).
