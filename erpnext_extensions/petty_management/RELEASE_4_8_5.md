# erpnext_extensions v4.8.5 — Release Notes

**Target:** Petty Management — Cancel PM Request business action

## Problem

Finance-approved PM Requests with no downstream financial usage were financially eligible to
cancel (v4.6.8 `request_lifecycle_eligibility`), but Desk exposed only Frappe's generic
**Cancel** button, which requires DocPerm `cancel`. Production Custom DocPerm and the PM User
role left real users without any cancel path.

## Solution

Dedicated business action **Cancel PM Request**, matching the architecture of
**Create Payment Entry** and **Close PM Request**:

- Visibility: `can_cancel_pm_request` in `compute_pm_request_action_flags()`
- Server: `cancel_pm_request()` reuses `assert_pm_request_cancel_allowed()` then `doc.cancel()`
- Permission: `user_may_execute_pm_request_cancel()` — Petty Management Accountant /
  operational PM visibility role / Administrator (not DocPerm `cancel`)
- Desk: custom toolbar button; generic Frappe Cancel suppressed on PM Request

## Unchanged

- v4.6.8 financial eligibility rules (`get_pm_request_cancel_blockers`)
- Workflow (no Cancel transition; `docstatus=2` via standard cancel)
- `on_cancel` / `before_cancel` controller hooks
