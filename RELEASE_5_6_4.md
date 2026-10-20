# RELEASE 5.6.4 — Secure Employee Checkin Excel Import

**App:** `erpnext_extensions`  
**Version:** `5.6.4`  
**Requires:** ERPNext ≥ 16.51.1 / Frappe ≥ 16.52.0 / HRMS ≥ 16.51.0  
**Verified on:** `development.localhost` only. Not deployed to Production.

---

## 1. What this release does

Historical attendance-device punches imported from Excel were rejected by a custom manual check-in limit. Desk entry of a historical check-in must keep that limit. Authorized device imports must not.

The Excel dialog stays the database Client Script `Employee Checkin Excel Import V1.0.1` (JavaScript V1.3.1). Parsing, Persian digits, Jalali conversion, employee matching, progress and the final report stay in that script. The server decides which inserts are trusted device imports.

Standard HRMS has **no physical attendance-device registry**. Offline punches belong on **Employee Checkin**. Online integrations continue to use HRMS `add_log_based_on_employee_field` with an external sync tool.

---

## 2. Manual limit (HR Settings)

Desk path: **HR → HR Settings → tab Shift and Attendance → heading Manual Attendance Checkin Policy**
(immediately under Allow Geolocation Tracking).

| Setting | Fieldname | Type | Default |
|---|---|---|---|
| Enable Manual Checkin Limit | `espad_enable_manual_checkin_limit` | Check | 1 |
| Maximum Manual Checkins per Period | `espad_max_manual_checkins` | Int | 5 |

- Period: Jalali **26th → 25th** (unchanged).
- Counted per **employee**.
- Check-ins dated **today** are not counted.
- Existing rows count as manual when `DATE(creation) ≠ DATE(time)`.
- Missing / null / invalid maximum **never** means unlimited: runtime falls back to enabled + 5.
- Explicit Enable = 0 disables only this quota (not HRMS validations).
- Fields use **permlevel 0** so they remain visible on Desk. **Only System Manager** may change them (server `validate` on HR Settings). HR Manager / HR User can see but not save policy changes.
- A Heading is used instead of a Section Break: Frappe places custom Section Breaks after the next tab when `insert_after` is the last field of Shift and Attendance.
- Request-local policy cache; cleared on HR Settings update.
- Assign / repair: `bench migrate` runs `ensure_hr_settings_fields()` idempotently.

---

## 3. Device vs manual exemption

| Action | Quota |
|---|---|
| Desk / `insert` / `insert_many` historical check-in | Applies (when enabled) |
| Authorized `import_device_checkins` (offline Excel) | Exempt (`device_import_context`) |
| Native `add_log_based_on_employee_field` | Exempt **only if** the user has role `Attendance Device Importer` |
| Same native method without that role | Quota applies |
| Client flags / `device_id` / forged JSON | Never grant exemption |

System Manager may use the Excel import API. System Manager alone does **not** exempt native `add_log` Desk-style inserts; assign `Attendance Device Importer` to the integration user.

---

## 4. Import role and permissions

Role: **`Attendance Device Importer`** (created on migrate; **not** auto-assigned).

Also allowed: **System Manager**.

Additionally required: Create on Employee Checkin, Read on each Employee (User Permissions apply). Write on Employee only when filling empty `attendance_device_id`.

Assign the role: **User → Roles → Attendance Device Importer**.

---

## 5. Identifier mapping

| Value | Field |
|---|---|
| Excel employee / device user code | `Employee.attendance_device_id` (optional empty-only fill) |
| Resolved employee | `Employee Checkin.employee` |
| Physical terminal / site | `Employee Checkin.device_id` (optional dialog **Location / Device ID**) |

The importer **never** writes the employee code into `Employee Checkin.device_id`. If neither Excel nor the dialog supplies a real terminal ID, `device_id` stays empty. Conflicting `attendance_device_id` assignments are rejected, not overwritten.

Read-only review of historical rows where `device_id == attendance_device_id`:

`erpnext_extensions.extentionhrms.employee_checkin_import.report_employee_code_stored_as_device_id`

No automatic mass cleanup of existing rows is performed.

---

## 6. Performance (development.localhost)

File `shahrivar.xlsx`: 11,900 check-in payloads.

| Measure | Result |
|---|---|
| New records (first full import) | 10,636 |
| Failed | 0 |
| Wall clock | ~60 seconds |
| Batch size | 200 (hard server cap) |
| Second pass | 0 creates (idempotent) |

---

## 7. Duplicate prevention

Application checks + unique key `espad_checkin_emp_time_log` on `(employee, time, log_type)` when the table has no duplicate groups. If duplicates already exist, migrate logs and **skips** the index; it does not delete rows.

---

## 8. Server Script compatibility

`# Employee Checkin - Outside Shift Day Limit` is disabled only when it matches the known sha256 `0f5676dcf01eea441343adee8043eab76a377a625602beae9b4d6438e3042c2e` or already carries the retirement marker. Unknown customizations are left enabled and logged.

---

## 9. Deployment

1. Deploy app at **5.6.4**.
2. `bench --site <site> migrate` then `clear-cache`.
3. Confirm HR Settings fields and defaults.
4. Assign `Attendance Device Importer` to offline importers and to online integration users that call `add_log_based_on_employee_field`.
5. Open Employee Checkin list → **Import Attendance (Excel)**.
6. Optional: set **Location / Device ID** in the dialog for the physical terminal.

---

## 10. Rollback

Revert the 5.6.4 commits on `develop` and migrate so the Client Script is rewritten from the previous file. Re-enable the legacy Server Script only if the Python hook is also removed. Do not leave both enabled.

---

## 11. Existing data notes

Sites that imported Excel when `device_id` was incorrectly set to the personnel code may have historical rows where `device_id == attendance_device_id`. Those rows are left unchanged. Use the read-only report above; any cleanup must be a separate audited migration with dry-run approval.

Shift Assignment / Auto Attendance configuration is out of scope for this importer.
