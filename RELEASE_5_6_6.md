# RELEASE 5.6.6 — Employee Checkin Excel Import Existing-Record Lookup

**App:** `erpnext_extensions`  
**Version:** `5.6.6`  
**Requires:** ERPNext ≥ 16.51.1 / Frappe ≥ 16.52.0 / HRMS ≥ 16.51.0  
**Verified on:** `development.localhost`

---

## 1. Summary

Fixes the disabled **Import** button on Employee Checkin → Import Attendance (Excel) after employee matching succeeded.

Symptom on affected sites:

```text
[OK] Employee matching completed.
[INFO] Checking existing records…
[ERROR] Employee matching failed.
```

Ready could show the full punch count while Duplicates stayed `0` and Import remained disabled.

---

## 2. Root cause

1. Pre-import duplicate detection used Desk `frappe.desk.reportview.get_list` on **Employee Checkin** (often as HTTP GET with a large `employee in (...)` filter).
2. Role **Attendance Device Importer** was created by migrate but had **no Custom DocPerm**, so callers could match Employees yet fail `get_list` with `PermissionError`.
3. The Client Script wrapped matching **and** the existing-record phase in one `catch`, so duplicate-check failures were mislabeled as “Employee matching failed.”

Unmatched personnel and invalid Excel rows were not the cause.

---

## 3. What this release does

### Secure existing-checkin lookup API

New whitelisted method:

`erpnext_extensions.extentionhrms.employee_checkin_import.find_existing_checkin_keys`

- Requires an authorized import caller (`System Manager` or `Attendance Device Importer`) **and** Employee Checkin **Create**.
- Accepts up to **2000** `{employee, time, log_type}` keys per request (client chunks larger files).
- Returns only matching keys — never full Employee Checkin documents.
- Enforces Employee **read** / User Permissions; keys for unreadable employees are omitted.
- Uses the same identity as HRMS / device import: employee + second-precision time + log type.

### Permission corrections

`ensure_import_role_permissions()` (runs on migrate / setup) grants **Attendance Device Importer** on Employee Checkin only:

| Permission | Value |
|---|---|
| Read | 1 |
| Create | 1 |
| Select | 1 |
| Write / Delete / Export / Import / Submit | 0 |

Idempotent: updates the existing Custom DocPerm when present.

### Client Script V1.3.2

`Employee Checkin Excel Import V1.0.1` is synced from:

`extentionhrms/public/js/employee_checkin_excel_import.js`

Changes:

- Duplicate detection calls `find_existing_checkin_keys` (POST).
- Remaining Desk `get_list` calls use POST (avoids URI-too-large risks).
- Separate error text: **Existing-record check failed** vs **Employee matching failed**.
- Import stays disabled when the existing-record check fails or when there are no new rows.

### Duplicate detection

- Pre-check and import share the same key semantics.
- Repeating a batch remains idempotent; no historical `device_id` rewrite.

### Security considerations

- Lookup API is not a general Employee Checkin dump.
- Unauthorized users (no import role or no Create) receive `PermissionError`.
- Employee User Permissions continue to scope readable employees.
- Import still requires the dedicated role; Create alone is not enough.

---

## 4. Code changes

| File | Change |
|------|--------|
| `erpnext_extensions/__init__.py` | `__version__ = "5.6.6"` |
| `extentionhrms/employee_checkin_import.py` | `find_existing_checkin_keys`, `_readable_employees` |
| `extentionhrms/employee_checkin_import_setup.py` | `ensure_import_role_permissions` |
| `extentionhrms/public/js/employee_checkin_excel_import.js` | Client Script **V1.3.2** |
| `extentionhrms/tests/test_employee_checkin_device_import.py` | Lookup / permission regression tests |
| `RELEASE_5_6_6.md` | This document |

`RELEASE_5_6_5.md` remains the historical payroll-accrual release note and is not rewritten by this release.

---

## 5. Migration / deployment

On each site after updating the app to **5.6.6**:

```bash
bench --site <site> migrate
bench --site <site> clear-cache
```

Migrate is expected to:

- Ensure role `Attendance Device Importer`.
- Ensure Custom DocPerm Create/Read/Select on Employee Checkin for that role.
- Sync Client Script `Employee Checkin Excel Import V1.0.1` to **V1.3.2**.
- Leave HR Settings policy fields and the unique checkin index unchanged when already present.

Setup is idempotent and does not create duplicate roles, permissions, or Client Scripts.

### Verification checklist

- [ ] `erpnext_extensions.__version__` is `5.6.6`
- [ ] Client Script body contains `Version: V1.3.2` and `find_existing_checkin_keys`
- [ ] Custom DocPerm exists for Attendance Device Importer on Employee Checkin (read/create/select)
- [ ] Excel pre-import: matching OK → existing-record check OK → Import enabled when fresh rows exist
- [ ] When all punches already exist: Import disabled, duplicate count accurate, no error banner
- [ ] Unauthorized user cannot call `find_existing_checkin_keys` or `import_device_checkins`
- [ ] No unintended rewrite of historical Employee Checkin rows during pre-check

Do **not** re-import a full historical Excel merely to validate the pre-check.

---

## 6. Tests

```bash
bench --site development.localhost run-tests --app erpnext_extensions --module erpnext_extensions.extentionhrms.tests.test_employee_checkin_device_import
```

Covered cases include authorized lookup, unauthorized rejection, import-role Create requirement, and duplicate key reporting without modifying rows.
