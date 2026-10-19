# RELEASE 5.6.4 — Secure Employee Checkin Excel Import

**App:** `erpnext_extensions`  
**Version:** `5.6.4`  
**Branch:** `feature/5.6.3-secure-checkin-excel-import`  
**Requires:** ERPNext ≥ 16.51.1 / Frappe ≥ 16.52.0 / HRMS ≥ 16.51.0  
**Verified on:** `development.localhost` only. Not deployed to Production.

---

## 1. What this release does

Historical attendance-device punches imported from Excel were rejected by the manual check-in limit: five historical Employee Checkins per employee per Jalali period (the 26th through the 25th). Desk entry of a historical check-in must keep that limit. An authorized device import must not.

The Excel dialog stays the database Client Script `Employee Checkin Excel Import V1.0.1` (JavaScript V1.3.0). Parsing, Persian digits, Jalali conversion, employee matching, progress and the final report stay in that script. The server decides which inserts are device imports.

---

## 2. Manual limit and device import

| Action | Limit |
|---|---|
| Desk or `insert` / `insert_many` of a historical check-in | 5 per employee per Jalali 26–25 period |
| Check-in dated today | Not counted |
| Authorized call to `import_device_checkins` | Limit is not applied and these rows do not need a client flag |

IN and OUT each count. The period is taken from the check-in date. HRMS still checks an active employee, the timestamp, log type and its own duplicate rule.

A browser field, `device_id`, or a JSON flag cannot open this exception. Frappe 16.52 runs the old Server Script inside `safe_exec`, where `frappe.flags` is an empty object, so the exception lives in `extentionhrms.employee_checkin_manual_limit` and is entered only by `device_import_context()` inside the whitelisted API.

---

## 3. API and permissions

`erpnext_extensions.extentionhrms.employee_checkin_import.import_device_checkins`

- Logged-in user only
- Role `System Manager` or `Attendance Device Importer`
- Create permission on Employee Checkin
- Read permission on each Employee, including User Permissions
- At most 200 records per request
- Active employees, `IN` or `OUT`, parseable timestamp
- Duplicate key: employee + timestamp + log type
- One failed row rolls back to a savepoint; the rest of the batch continues
- Empty `attendance_device_id` can be filled. A non-empty value is left as it is

`Attendance Device Importer` is created on migrate. It is not granted to existing users. System Manager can import without that role. Everyone else who should import must be given the role. Create permission alone is not enough.

---

## 4. Performance measured on development.localhost

File `shahrivar.xlsx`: 11,900 check-in payloads, 1,264 already stored.

| Measure | Result |
|---|---|
| New records inserted | 10,636 |
| Failed | 0 |
| Wall clock | 60 seconds |
| API time | 59.88 seconds |
| Requests | 54, all HTTP 200 |
| Batch size | 200 |
| Batch latency P50 / P95 | 1,125 ms / 1,236 ms |
| Rate | 177.6 records/second |

The second pass detected all 11,900 rows as already imported and created none.

---

## 5. Duplicate prevention

The API checks existing rows and rows repeated inside the batch. A unique database key `espad_checkin_emp_time_log` on `(employee, time, log_type)` stops two overlapping requests from inserting the same punch.

If that key is missing and duplicate groups already exist, migrate does **not** fail and does **not** delete rows. It logs `Employee Checkin duplicate index skipped` and leaves the index absent. Application-level duplicate checks still run. Add the index only after those groups are reviewed by hand.

---

## 6. Server Script compatibility

Database script `# Employee Checkin - Outside Shift Day Limit` is disabled only when its source is the known script (sha256 `0f5676dcf01eea441343adee8043eab76a377a625602beae9b4d6438e3042c2e`) or it already contains the retirement marker. The original text is kept in `extentionhrms/legacy/employee_checkin_outside_shift_day_limit.server_script.txt`.

A different script is not disabled. Device import will still be rejected by that script until someone reviews it. Migrate records the mismatch in the Error Log and continues.

Other Employee Checkin Server Scripts (shift resolver, attendance mapper, rest attendance) are unchanged.

---

## 7. Deployment

1. Deploy the app at `5.6.4`. Do not deploy this revision to Production until it is explicitly approved.
2. `bench --site <site> migrate` then `clear-cache`.
3. Confirm the legacy Server Script is disabled, or read the Error Log if the hash did not match.
4. Assign role `Attendance Device Importer` to importers who are not System Manager.
5. Open Employee Checkin list. The button is `Import Attendance (Excel)`. HRMS 16.51 marks this DocType `in_create`; the button checks both `can_create` and `in_create`.
6. Leave `server_script_enabled` on. The other check-in scripts still need it.

Client Script content is copied from `extentionhrms/public/js/employee_checkin_excel_import.js` into `Employee Checkin Excel Import V1.0.1` on migrate. Do not enable the older disabled importer scripts.

---

## 8. Rollback

Revert the `5.6.4` merge on `develop` and migrate again so the Client Script is rewritten from the previous file.

Re-enable `# Employee Checkin - Outside Shift Day Limit` only if the Python hook is also removed. Leaving both enabled applies the limit twice and blocks device import again.

The unique index does not change existing rows. Dropping it is optional and is not required to restore the manual limit.

---

## 9. Production configuration required before use

- Role `Attendance Device Importer` for non-System Manager importers
- Confirmation that the manual-limit Server Script is the known script, or a reviewed replacement
- No duplicate `(employee, time, log_type)` groups if the unique index must be created
- Scheduler and outbound mail remain under the site's existing isolation; this release does not change them
