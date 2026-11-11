# Release 5.7.30 — Desk Module: بهای تمام شده (Costing)

**Version:** **5.7.30**  
**Site:** `development.localhost`  
**Deployment:** Local Development only — **no Production deploy**

## Requirement

Independent ERPNext Desk tile **بهای تمام شده** that opens the dedicated Costing navigation shell (not a Workspace-only shortcut).

## Implemented

| Layer | Name | Notes |
|---|---|---|
| Module Def | `Costing` | App `erpnext_extensions`; all Costing DocTypes remain here |
| Display / Desk tile | `بهای تمام شده` | Desktop Icon, icon `calculator`, roles: System Manager, Accounts Manager/User, Manufacturing Manager, Auditor |
| Sidebar (v16 nav) | `بهای تمام شده` | `module=Costing`, 21 Persian links in 4 sections |
| Workspace | `بهای تمام شده` | Public home target for sidebar “خانه” |
| Fixture | `costing/sidebar/بهای تمام شده/بهای تمام شده.json` | Standard sidebar for migrate |
| Patch | `ensure_costing_desk_module_v5730` | Idempotent seed on migrate / `ensure_now()` |

### Workspace / Sidebar sections

1. **تنظیمات** — Settings / account-dimension rules / Primary UOM  
2. **محاسبات ماهانه** — Monthly FG Cost Calculation list & status  
3. **گزارش‌ها و جزئیات** — Expense Pools, GL lines, batch/product allocations, UOM  
4. **مدیریت و سوابق** — Revision history & lock status  

## Acceptance checks (`development.localhost`)

1. Desktop Icon `بهای تمام شده` exists, `hidden=0`, `standard=1`, `link_to=بهای تمام شده`  
2. With bootinfo, icon is permitted and carries `module=Costing`  
3. Module Def `Costing` is visible; empty duplicate Module Def `بهای تمام شده` removed  
4. Sidebar item count = 21  
5. Existing modules (Manufacturing, Stock, Accounting, Facility Management) unchanged  

## Notes

- Frappe v16 Desk Icons route through `desktop_icon.module` → module Sidebar (Workspace Sidebar is an inert archive).  
- Child tables (Expense Rules, Item Primary UOM) open via Settings form links.  
