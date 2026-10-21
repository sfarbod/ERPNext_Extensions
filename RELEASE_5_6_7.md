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

### UI (Client Script V2.0.1)

- Synced from `extentionhrms/public/js/attendance_excel_import.js`.
- Summary stats (personnel / matched / unmatched / ambiguous / rows / invalid).
- Timing log (library / parse / match / preload / existing / import).
- **Local SheetJS** `xlsx@0.18.5` at `/assets/erpnext_extensions/js/lib/xlsx.full.min.js` (no runtime CDN).
- Server-side personnel code index (avoids client `get_list` default limit=20 and permlevel-1 stripping of `custom_personal_code`).
- Bulk existing-record check and schedule preload before import.
- Batched save/submit via the new API (50 rows/batch default).
- Errors show the failing phase and asset URL / cause.

### Backend APIs

| Method | Purpose |
|--------|---------|
| `get_personnel_code_index` | Authorized `{normalized_code: [employee,…]}` map for matching |
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

Verify Client Script contains `Version: V2.0.1`, local SheetJS asset path, and `import_attendance_details`.

### Correction (V2.0.1 within 5.6.7)

1. **Excel library load:** Replaced jsDelivr CDN injection with the vendored SheetJS `xlsx@0.18.5` under `erpnext_extensions/public/js/lib/xlsx.full.min.js`. The Desk error `Excel library could not load` was `script.onerror` on `https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js` (CDN/CSP/network). Local asset returns HTTP 200 and exposes `window.XLSX.read`.
2. **Employee matching:** Client `frappe.db.get_list` ignores `limit_page_length` and defaults to `limit=20`, so only ~20 Employees were indexed. Matching now uses authorized `get_personnel_code_index`.
3. **UX:** Timing log replaces the empty progress strip; errors show the failing phase; Refresh Preview uses `btn-default` so it does not collide with the dialog primary action (`btn-modal-primary` on Frappe 16).

### Browser E2E on `development.localhost` (`mehr.xls`)

| Phase | Result |
|-------|--------|
| Parse | 7994 Excel rows, 0 invalid |
| Match | 261 personnel, 261 matched, 0 unmatched, 0 ambiguous |
| Timings (typical) | Library ~35–55 ms · Parse ~640–900 ms · Match ~15–130 ms · Preload ~40–155 ms · Existing ~15–90 ms |
| Small import (2 employees) | Created 45 · Skipped 17 · Failed 0 · **1126 ms (~40 rec/s)** |
| Larger import (30 employees / 930 daily rows) | Created 663 · Updated 45 · Skipped 206 · Failed 16* · **16703 ms (~42 rec/s)** |
| Integrity | No duplicate (employee, date); Employee Checkin unchanged; Leave Application unchanged |

\*Failed rows are intentional business-rule refusals (historical Shift Assignment gaps / missing schedule assignment), not library or API crashes.

Screenshots (populated UI + import result): `extentionhrms/e2e/screenshots/attendance_excel_import/`.

---

## 6. Tests

```bash
# Prefer direct unittest load if unrelated apps break bench discovery:
cd sites && ../env/bin/python - <<'PY'
import frappe, unittest
frappe.init(site="development.localhost"); frappe.connect(); frappe.set_user("Administrator")
from erpnext_extensions.extentionhrms.tests import test_attendance_excel_import as m
unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(m))
PY
```

Covers existing lookup, cancelled exclusion, unauthorized rejection, create+submit, idempotent submitted update, preload, Client Script sync (V2.0.1 + local SheetJS path, no CDN), local SheetJS asset presence, personnel-code normalization/index, and permission gates.

Playwright regression helper: `extentionhrms/e2e/attendance_excel_import_playwright.mjs` (library HTTP 200 + file select + parse stats).

---

## 7. Known limitations

- Full shift-rotation math remains in the Client Script (now fed by preload cache). Complex rotation edge cases still surface as row failures rather than silent guesses.
- Server-side submit hooks may still recalculate some hour fields; discrepancies are reported when detected.
- Playwright coverage depends on Desk availability in the environment.
- Do not run a full historical re-import on Production without a backup and an explicit window.
