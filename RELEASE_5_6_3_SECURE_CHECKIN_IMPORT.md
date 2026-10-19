# RELEASE 5.6.3 addendum — Secure Employee Checkin device import

**App:** `erpnext_extensions`  
**Version:** `5.6.3` (unchanged)  
**Branch:** `feature/5.6.3-secure-checkin-excel-import`  
**Site used for verification:** `development.localhost` only  
**Not deployed to Production.**

## Root cause

Excel import of historical attendance-device punches was rejected by the database Server Script `# Employee Checkin - Outside Shift Day Limit`. That script treats every Employee Checkin whose date is not today as a manual/delayed entry and allows five such entries per employee per Jalali period (26th through 25th). Frappe runs Server Scripts inside `safe_exec`, which exposes an empty `frappe.flags`, so the script cannot tell an authorized device import from a Desk entry.

The previous client then pre-skipped those rows (1,264 created, 10,636 skipped). Classifying a row as a device import only because the timestamp is historical would have been wrong, and a client flag such as `skip_manual_limit` would have been an insecure bypass.

## Policy

Ordinary Employee Checkin creation still enforces the five-entry manual limit. The rule now lives in `erpnext_extensions.extentionhrms.employee_checkin_manual_limit` on `Employee Checkin.before_insert`.

Authorized device import uses `import_device_checkins`. The method checks the session, requires Employee Checkin create permission, and requires role `System Manager` or `Attendance Device Importer`. Only then does it enter `device_import_context()`, a `contextvars` token the browser cannot set. The context is cleared before the method returns. Inside that context the manual-count restriction is skipped. HRMS still validates the employee, the timestamp, duplicates (`employee + time + log_type`), and permissions.

The legacy Server Script is disabled only when its source still matches the known production hash, or it already carries the retirement marker. `after_migrate` repeats this so a database restore does not turn the sandboxed script back on beside the hook. A script whose source does not match is left unchanged and logged.

## Client

`Employee Checkin Excel Import V1.0.1` JavaScript is **V1.3.0**. The source of truth is `erpnext_extensions/extentionhrms/public/js/employee_checkin_excel_import.js`. Migrate copies it into the existing Client Script. The dialog still parses Jalali dates and Persian digits, matches active employees, and keeps IN/OUT. It sends batches to the authorized API. Duplicates are not counted as failures. Empty Attendance Device ID values can still be filled; non-empty values are not overwritten.

## Duplicate handling and performance

The server locks each employee for the batch and releases the lock only after commit. A unique key on `Employee Checkin (employee, time, log_type)` stops two overlapping requests from inserting the same punch. Repeating the same file is idempotent. The client reconciles the database before retrying a dropped request.
