# Release 5.3.2 — Asset Scrap / Restore RPC Boundary

Package version: **5.3.2**.

Hardens the Asset Usage / Depreciation whitelist wrappers so Frappe Desk RPC
can scrap and restore assets without forwarding dispatch metadata into native
ERPNext.

---

## Asset Scrap — unexpected keyword argument `cmd`

Fixed:

`TypeError: scrap_asset() got an unexpected keyword argument 'cmd'`

when using the standard ERPNext **Scrap Asset** action on the Asset form.

### Root cause

Desk calls the whitelisted method via `frappe.call(method, **form_dict)`.
`form_dict` always includes Frappe RPC dispatch metadata such as `cmd`.

`erpnext_extensions` overrides:

- `erpnext.assets.doctype.asset.depreciation.scrap_asset`
- `erpnext.assets.doctype.asset.depreciation.restore_asset`

The extension wrappers previously used `*args, **kwargs`. Because of `**kwargs`,
`frappe.get_newargs` does **not** strip unsupported keys, so `cmd` reached the
wrapper and was forwarded into native ERPNext `scrap_asset(asset_name, scrap_date)`,
which rejects unknown keyword arguments.

### Fix

- Align `scrap_asset` and `restore_asset` wrapper signatures with native ERPNext
  (fixed parameters; no `**kwargs` forwarding).
- Frappe therefore strips RPC metadata at the compatibility boundary.
- Only business arguments (`asset_name`, `scrap_date`) reach native scrap/restore.
- Asset Usage / Depreciation post-processing (`_reapply_for_asset`) is preserved.
- Does **not** modify ERPNext or Frappe core.
- Does **not** modify Persian Calendar behavior (Jalali display remains unrelated).

### Out of scope

Company **Asset Depreciation Cost Center** (`depreciation_cost_center`) master-data
configuration is **not** addressed in v5.3.2. That is a separate pre-existing
company setup concern and must not be conflated with this RPC-boundary fix.

---

## Regression coverage

Added focused RPC-boundary tests that prove:

- `cmd` is stripped before the wrapper / native call
- business args still reach native scrap/restore
- extension reapply still runs exactly once
- calls without `cmd` continue to work

---

## Deployment

| Step | Required |
|------|----------|
| `bench migrate` | No |
| Patch / schema | No |
| Asset build | No |
| Cache clear | Recommended |
| Restart | Recommended |

`erpnext_extensions.__version__` = **5.3.2**
