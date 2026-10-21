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

### UI (Client Script V2.0.4)

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

Verify Client Script contains `Version: V2.0.4`, local SheetJS asset path, `data-espad-att-progress`, `unauthorizedNormalOT`, `custom_extra_presence_hours`, and `import_attendance_details`.

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
| 4 | کل مأموریت | `custom_mission_work_hours` |
| 5+6+7 | خروج غیرمجاز + تعجیل + تأخیر | `custom_work_shortage_hours` |
| 8 | غیبت > 0 | `status = "Absent"` |
| 14 | تعطیل مجاز | `custom_holiday_work_hours` |
| 15 | عادی غیرمجاز | `custom_regular_work_hours` **and** `custom_extra_presence_hours` (identical) |
| 10+12+14+16 | Approved OT components | `custom_approved_overtime_hours` |
| 17 | حضور | `working_hours` |

#### Rules

1. **Work shortage** = sum of Excel unauthorized exit + early + late (minutes → hours). Not shift math; absence excluded.
2. **Dual unauthorized OT:** Excel عادی غیرمجاز alone populates both `custom_regular_work_hours` and `custom_extra_presence_hours` with the same decimal hours (including explicit zero). Overrides the prior sum-of-all-unauthorized-OT mapping for `custom_regular_work_hours`. Does not feed Approved OT.
3. **Absence:** Excel غیبت duration > 0 forces `status = "Absent"` over punch-inferred Present.
4. **Approved OT:** unchanged V2.0.3 formula; OTAR cannot replace after submit.
5. **Persistence:** `EXCEL_AUTHORITATIVE_FIELDS` extended with `status`, `working_hours`, `custom_night_work_hours`, `custom_work_shortage_hours`, dual OT fields, etc., and re-applied after create/draft submit.

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

Covers existing lookup, cancelled exclusion, unauthorized rejection, create+submit, idempotent submitted update, preload, Client Script sync (V2.0.4 + local SheetJS path, no CDN), local SheetJS asset presence, personnel-code normalization/index, permission gates, Approved Overtime Hours formula, work-shortage sum, dual unauthorized-OT fields, absence→Absent, create/draft/submitted persistence, and post-submit re-apply.

Playwright regression helper: `extentionhrms/e2e/attendance_excel_import_playwright.mjs` (library HTTP 200 + file select + parse stats).

---

## 7. Known limitations

- Full shift-rotation math remains in the Client Script (now fed by preload cache). Complex rotation edge cases still surface as row failures rather than silent guesses.
- Server-side submit hooks may still recalculate some hour fields; discrepancies are reported when detected.
- Playwright coverage depends on Desk availability in the environment.
- Do not run a full historical re-import on Production without a backup and an explicit window.
