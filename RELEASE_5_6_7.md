# RELEASE 5.6.7 — Attendance Details Excel Import Redesign

**App:** `erpnext_extensions`  
**Version:** `5.6.7`  
**Requires:** ERPNext ≥ 16.51.1 / Frappe ≥ 16.52.0 / HRMS ≥ 16.51.0  
**Verified on:** `development.localhost`

---

## 1. Summary

Redesigns **Import Attendance Details (Excel)** on the Attendance List View for ERPNext/HRMS v16.

The previous Client Script performed sequential per-row Desk API calls for employee matching, existing Attendance lookup, shift/schedule resolution, insert/save/submit, and post-read. On files such as `mehr.xls` (~8k daily rows / ~260 personnel) this made the UI unresponsive and imports extremely slow.

This release keeps the Attendance business rules (custom hours, shift rotation, submitted-field updates) while adopting the Employee Checkin importer’s bulk patterns: index matching, server-side lookups, schedule preload, and batched persistence.

---

## 2. Root causes of poor performance

1. Per-person `get_list` during matching despite an employee index.
2. Existing Attendance fetched in employee chunks of 20 with pagination.
3. `resolveSchedule` called `frappe.client.get` for Shift Assignment children, Shift Schedule, Shift Type, and Holiday List **per row**.
4. Each import row: get_list + get/save/insert + submit + get again.
5. No bounded server batch endpoint for Attendance details.

---

## 3. What changed

### UI (Client Script V2.0.0)

- Synced from `extentionhrms/public/js/attendance_excel_import.js`.
- Summary stats (personnel / matched / unmatched / ambiguous / rows).
- Compact progress log area.
- SheetJS CDN load for `.xls` / `.xlsx` (unchanged compatibility).
- Index-only personnel matching on `Employee.custom_personal_code`.
- Bulk existing-record check and schedule preload before import.
- Batched save/submit via the new API (50 rows/batch default).

### Backend APIs

| Method | Purpose |
|--------|---------|
| `find_existing_attendance` | Authorized bulk (employee, date) lookup; skips cancelled; flags On Leave |
| `preload_attendance_import_context` | Bulk Shift Assignment / Schedule / Shift Type / Holiday List payload |
| `import_attendance_details` | Batch create / draft update / submitted-field update (max 100/call) |

Authorization: **System Manager** or **HR Manager**, plus Attendance Create. Employee User Permissions apply to lookups and writes.

Submitted updates still only touch the permitted field allow-list (same policy as the legacy `espad_attendance_excel_update_submitted` Server Script).

### Setup

`ensure_attendance_excel_import_setup()` on `after_migrate` syncs Client Script `Employee Attendance Excel Import` idempotently.

---

## 4. Unchanged business rules

- Excel → `custom_personal_code` matching (zero-padding aware).
- Jalali / Persian digit normalization.
- Create + Submit for new/Draft rows; controlled field update for Submitted.
- No silent overwrite of On Leave Attendance.
- No Employee Checkin creation.
- No raw SQL Attendance inserts.
- Night hours from actual IN/OUT overlap (22:00–06:00), not Excel night totals.

---

## 5. Migration / deployment

```bash
bench --site <site> migrate
bench --site <site> clear-cache
```

Verify Client Script contains `Version: V2.0.0` and `import_attendance_details`.

---

## 6. Tests

```bash
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.extentionhrms.tests.test_attendance_excel_import
```

Covers existing lookup, cancelled exclusion, unauthorized rejection, create+submit, idempotent submitted update, preload, Client Script sync, and no Employee Checkin side effects.

---

## 7. Known limitations

- Full shift-rotation math remains in the Client Script (now fed by preload cache). Complex rotation edge cases still surface as row failures rather than silent guesses.
- Server-side submit hooks may still recalculate some hour fields; discrepancies are reported when detected.
- Playwright coverage depends on Desk availability in the environment.
- Do not run a full historical re-import on Production without a backup and an explicit window.
