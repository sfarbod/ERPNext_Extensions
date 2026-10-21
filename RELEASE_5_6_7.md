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

### UI (Client Script V2.0.7)

- Synced from `extentionhrms/public/js/attendance_excel_import.js`.
- Summary stats (personnel / matched / unmatched / ambiguous / rows / invalid).
- Timing log (library / parse / match / preload / existing / import).
- **Local SheetJS** `xlsx@0.18.5` at `/assets/erpnext_extensions/js/lib/xlsx.full.min.js` (no runtime CDN).
- Server-side personnel code index (avoids client `get_list` default limit=20 and permlevel-1 stripping of `custom_personal_code`).
- Bulk existing-record check and schedule preload before import.
- Batched save/submit via the new API (50 rows/batch default).
- Errors show the failing phase and asset URL / cause.
- **Inline Import Progress** inside the original dialog (progress bar, %, batch, created/updated/skipped/failed from backend, scrollable log). No nested `frappe.show_progress` / result dialog. Close confirms while a run is active.
- **Excel-authoritative preview columns:** Status, Paid/Unpaid Leave, Night, Mission, Shortage, Regular Work, Extra Presence, Approved OT, Holiday, Working Hours.

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

Verify Client Script contains `Version: V2.0.7`, `rejectOutlierWorkbookYears`, `Mission and absence conflict`, mission→Present, and `import_attendance_details`.

### Correction (V2.0.1 within 5.6.7)

1. **Excel library load:** Replaced jsDelivr CDN injection with the vendored SheetJS `xlsx@0.18.5` under `erpnext_extensions/public/js/lib/xlsx.full.min.js`. The Desk error `Excel library could not load` was `script.onerror` on `https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js` (CDN/CSP/network). Local asset returns HTTP 200 and exposes `window.XLSX.read`.
2. **Employee matching:** Client `frappe.db.get_list` ignores `limit_page_length` and defaults to `limit=20`, so only ~20 Employees were indexed. Matching now uses authorized `get_personnel_code_index`.
3. **UX:** Timing log replaces the empty progress strip; errors show the failing phase; Refresh Preview uses `btn-default` so it does not collide with the dialog primary action (`btn-modal-primary` on Frappe 16).

### Correction (V2.0.2 within 5.6.7)

1. **Inline progress:** Removed the separate `Attendance Import` progress dialog (`frappe.show_progress`) and the nested `Attendance Import Result` dialog. Progress bar, batch status, backend counters, and the final result/log stay inside `Import Attendance Details (Excel)`, matching Employee Checkin Excel Import UX.
2. **Close policy:** While a synchronous import is running, Close / modal-X asks for confirmation; closing does not cancel in-flight server requests.
3. **Known import failures:** Rows that fail with historical Shift Assignment gaps / missing Shift Schedule Assignment remain **Failed** (not silently skipped or counted as success).

### Correction (V2.0.3 within 5.6.7) — Approved Overtime Hours regression

1. **Business formula restored:** `custom_approved_overtime_hours` =
   `(Night OT Approved + Mission OT Approved + Holiday OT Approved + Normal OT Approved) / 60`
   (Excel columns K/M/O/Q → minutes after duration parse → decimal hours), matching the legacy Client Script.
2. **Frontend:** Explicit assignment to `custom_approved_overtime_hours` in the batch payload (in addition to label-based `setHours`), so the field cannot be dropped when metadata lookup is ambiguous.
3. **Backend persistence:** After create/draft submit, Excel-authoritative hour fields (including Approved OT) are re-applied via `_reapply_excel_authoritative_fields` so `ESPAD ATT AFTER SUBMIT` / `espad_attendance_calculate` cannot silently replace Excel values with OTAR-derived zeros.
4. **Submitted path:** Controlled `update_submitted` allow-list already included `custom_approved_overtime_hours`; regression tests cover create, draft→submit, and submitted update persistence.
5. **Helpers/tests:** `approved_overtime_hours_from_components()` mirrors the legacy `hours(sum(minutes))` rounding; unit tests cover single/multi-component sums, zeros, Persian digits, and write paths.

### Correction (V2.0.4 within 5.6.7) — Excel-authoritative field mapping

Importer-only scope: does **not** change general Attendance / OTAR / Checkin Resolver logic.

#### Excel column → Attendance field matrix (`mehr.xls` rows 5–7)

| Index | Excel meaning | Attendance field |
|------:|---|---|
| 0 | مرخصی با حقوق | `custom_paid_leave_hours` |
| 1 | مرخصی بی‌حقوق | `custom_unpaid_leave_hours` |
| 2 | شبکاری کل | `custom_night_work_hours` |
| 4 | کل مأموریت | `custom_mission_work_hours` (+ `status = Present` when mission > 0) |
| 5+6+7 | خروج غیرمجاز + تعجیل + تأخیر | `custom_work_shortage_hours` |
| 8 | غیبت > 0 | `status = "Absent"` |
| 14 | تعطیل مجاز | `custom_holiday_work_hours` |
| 15 | عادی غیرمجاز | `custom_regular_work_hours` **and** `custom_extra_presence_hours` (identical) |
| 14+16 | تعطیل مجاز + عادی مجاز | `custom_approved_overtime_hours` |
| 17 | حضور | `working_hours` |

#### Rules

1. **Work shortage** = sum of Excel unauthorized exit + early + late (minutes → hours). Not shift math; absence excluded.
2. **Dual unauthorized OT:** Excel عادی غیرمجاز alone populates both `custom_regular_work_hours` and `custom_extra_presence_hours` with the same decimal hours (including explicit zero). Overrides the prior sum-of-all-unauthorized-OT mapping for `custom_regular_work_hours`. Does not feed Approved OT.
3. **Status:** Mission > 0 → `Present` (hours still mapped); absence > 0 → `Absent`; mission+absence → Review; presence without absence → `Present`. Blank absence / missing punches never force Absent. On Leave / Half Day preserved.
4. **Approved OT (V2.0.5+):** `Normal Approved + Holiday Approved` only. Night/Mission approved are excluded from this field (Holiday Approved still also maps to `custom_holiday_work_hours`).
5. **Persistence:** `EXCEL_AUTHORITATIVE_FIELDS` includes `status`, `working_hours`, dual OT, shortage, night, mission, approved OT, etc., re-applied after create/draft submit.

### Correction (V2.0.5 within 5.6.7) — Status + Approved OT

1. **Status bug:** Importer no longer defaults to Absent when punches are missing or absence is blank/zero. Only positive Excel غیبت forces Absent. Verified for رضوان مرزبانی / `1405/07/25` (Excel row 152, HR-EMP-0001, 2026-10-17): حضور `۱۱:۰۱`, غیبت blank → persisted `Present`.
2. **Approved OT formula:** Replaced four-category sum with `holidayOTApproved + normalOTApproved` (indexes 14 + 16). Night/Mission approved no longer inflate `custom_approved_overtime_hours`.

### Correction (V2.0.6 within 5.6.7) — Mission hours mapping

1. **Root cause:** Mission rows without punches were classified `Review` and skipped, leaving prior `Absent` with `custom_mission_work_hours = 0`.
2. **Interim:** Briefly used `On Duty` via a global Attendance class override (removed in V2.0.7).

### Correction (V2.0.7 within 5.6.7) — Mission Present + 1399 date audit

1. **Mission status:** Positive Excel `کل مأموریت` → `status = Present` with `custom_mission_work_hours` populated. No `On Duty`.
2. **Override removed:** Deleted `attendance_override.py` / `override_doctype_class` Attendance entry and the On Duty Property Setter. Native HRMS Attendance class restored.
3. **1399 dates:** List View Jalali `1399-*` for رضوان مرزبانی were **not** Excel parser failures. Stored Gregorian dates were `2020-09-21`…`2020-10-16` (Jalali 1399-06-31…1399-07-25) from earlier path-verification fixtures near `date_of_joining` (2020-08-22), not from `mehr.xls` (text Jalali `۱۴۰۵/…` cells). Real Excel imports for that employee remain `2026-09-17`…`2026-10-17` (Jalali 1405). The four fixture records were cancelled/deleted. Workbook majority-year validation (`rejectOutlierWorkbookYears`) blocks minority outlier Gregorian years without hardcoding 1405.
4. **Regression:** `1405/07/25` → `2026-10-17`. محسن اعتمادی / `1405/07/12` → Present / mission `9.0`.

### Browser E2E on `development.localhost` (`mehr.xls`)

| Phase | Result |
|-------|--------|
| Parse | 7994 Excel rows, 0 invalid |
| Match | 261 personnel, 261 matched, 0 unmatched, 0 ambiguous |
| Timings (typical) | Library ~35–55 ms · Parse ~640–900 ms · Match ~15–130 ms · Preload ~40–155 ms · Existing ~15–90 ms |
| Small import (2 employees) | Created 45 · Skipped 17 · Failed 0 · **1126 ms (~40 rec/s)** |
| Larger import (30 employees / 930 daily rows) | Created 663 · Updated 45 · Skipped 206 · Failed 16* · **16703 ms (~42 rec/s)** |
| Integrity | No duplicate (employee, date); Employee Checkin unchanged; Leave Application unchanged |
| Inline progress (V2.0.2) | No secondary progress modal; counters update per batch inside the original dialog |
| Inline E2E (15 employees) | Created 117 · Updated 223 · Skipped 116 · Failed 9* · **4003 ms (~85 rec/s)** · single modal only |

\*Failed rows are intentional business-rule refusals (historical Shift Assignment gaps / missing schedule assignment), not library or API crashes. They appear with Excel row numbers in the inline result panel.

**Approved OT E2E (V2.0.3):** Preview for personnel codes `3` and `15` showed Excel-derived Approved OT (e.g. row 115 → `14.4`, row 118 → `8.9`). Controlled import Updated 36 / Skipped 26 / Failed 0 in one modal with inline progress only. Post-import DB values for five multi-component Excel rows matched `(K+M+O+Q minutes)/60`.

**Excel-authoritative E2E (V2.0.4):** Expanded preview columns; Regular Work Hours === Extra Presence Hours for all previewed rows (dual عادی غیرمجاز). Import Updated 36 / Skipped 26 / Failed 0 with inline progress only. DB checks: row 231 عادی غیرمجاز `۸:۲۴` → both fields `8.4`; row 245 shortage (exit+early) → `1.983333`; synthetic absence create → `status=Absent`. Note: `mehr.xls` data rows have empty numeric غیبت / unpaid columns (header labels only).

**Status + Approved OT E2E (V2.0.5):** Personnel code `3` (رضوان مرزبانی). Preview row 152 / `2026-10-17` → Status `Present`, Approved OT `2.016667` (Normal `۲:۰۱` only). Rows 115/118/119 show corrected Approved OT excluding Night (`14.316667` / `6.933333` / `2.7` vs old four-sum). Import Updated 23 / Failed 0, single modal + inline progress. Post-import DB matched preview.

**Mission Present E2E (V2.0.7):** `mehr.xls` → 7994 rows / 261 matched; all workbook Jalali years = 1405 → Gregorian 2026. convertDate probes: `1405/07/25` / Persian / Arabic digits → `2026-10-17`. DB `HR-ATT-2026-14774` (محسن اعتمادی / `2026-10-04`): `Present` / mission `9.0`. `HR-ATT-2026-14573` (رضوان مرزبانی / `2026-10-17`): `Present`. Synthetic 2020 fixtures deleted; zero remaining `On Duty`. Inline progress present; no nested progress dialog.

Screenshots: `extentionhrms/e2e/screenshots/attendance_excel_import/v207_*.png`.

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

Covers existing lookup, cancelled exclusion, unauthorized rejection, create+submit, idempotent submitted update, Client Script sync (V2.0.7), mission→Present with hours, Jalali date regression (1405/07/25→2026-10-17), workbook year outlier rejection, Approved OT = Normal+Holiday, dual unauthorized OT, and post-submit re-apply.

Playwright regression helper: `extentionhrms/e2e/attendance_excel_import_playwright.mjs` (library HTTP 200 + file select + parse stats).

---

## 7. Known limitations

- Full shift-rotation math remains in the Client Script (now fed by preload cache). Complex rotation edge cases still surface as row failures rather than silent guesses.
- Server-side submit hooks may still recalculate some hour fields; discrepancies are reported when detected.
- Playwright coverage depends on Desk availability in the environment.
- Do not run a full historical re-import on Production without a backup and an explicit window.
