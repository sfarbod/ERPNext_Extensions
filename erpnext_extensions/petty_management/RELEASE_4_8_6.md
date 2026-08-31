# Release 4.8.6 — PM Request Cancel/Delete UX + Connections

## Summary

Finalizes PM Request lifecycle cleanup for v4.8.6:

- **Delete PM Request** is **Administrator only** (administrative cleanup). PM Accountant, PM User, and the operational visibility role cannot delete.
- **Cancel PM Request** remains a business action for PM Accountant and allowed roles when financial blockers are empty.
- **Toolbar UX**: primary actions stay on the toolbar (`Create Payment Entry`, `Close PM Request`); `Cancel PM Request` and `Delete PM Request` live under the **Actions** menu.
- **Connections tab** on PM Request shows downstream Payment Entries, PM Clearances, Journal Entries, and a usage summary sourced from authoritative funding services.

## Permission model

| Action | Who may execute | Business eligibility |
|--------|-----------------|----------------------|
| Cancel PM Request | Administrator, PM Accountant, operational PM visibility role | Finance-cleared submitted Request; no open PE / JE / Clearance blockers |
| Delete PM Request | **Administrator only** | Cancelled (docstatus 2); no PE, JE, Clearance, or other delete blockers |

Generic Frappe Cancel/Delete are suppressed on the PM Request form; business actions replace them.

## API / services

- `user_may_execute_pm_request_delete()` — Administrator only
- `delete_pm_request()` — whitelisted business delete (DocPerm-independent)
- `get_pm_request_connections()` — Connections tab payload via `request_connections_service`

## Tests

- `test_pm_request_delete_action_v486`
- `test_pm_request_connections_v486`
- Playwright: `playwright_pm_request_finalize_v486.mjs`

## Version

`erpnext_extensions.__version__` = **4.8.6**
