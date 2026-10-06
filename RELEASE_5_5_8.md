# erpnext_extensions 5.5.8

## Asset Connections Asset Movement dedupe + Asset Request duplicate policy

### Summary

1. **Asset Request duplicate policy default** — `Asset Request Settings.prevent_duplicate_active_requests` default is now **0** (allow multiple active Asset Requests for the same employee + item). The optional setting remains: when set to **1**, the legacy blocking guard still runs.

2. **Asset Connections — duplicate Asset Movement** — A site-custom DocType Link (`group=Asset Movement`, `link_fieldname=asset_name`) duplicated ERPNext’s native **Movement → Asset Movement** connection and overwrote the correct filter (`asset` → `asset_name`), so Connections counts showed **0** despite real movements on `Asset Movement Item.asset`.

3. **Migration cleanup** — Idempotent patches `remove_duplicate_asset_movement_doctype_link_v558` / `v558b` remove only custom Asset → Asset Movement DocType Links that match the known-invalid signature (`parent=Asset`, `parenttype` in `{DocType, Customize Form}`, `custom=1`, group `Asset Movement`, field `asset_name`). Customize Form rows are the usual store for Desk customizations. **Equipment Profile** and other custom links are preserved.

4. **Dashboard hardening** — `asset_usage_depreciation.asset_dashboard.get_data` still adds **Usage → Asset Usage Period** and **Request → Asset Request**, then sanitizes the merged payload so Asset Movement appears **once** under **Movement** with `non_standard_fieldnames["Asset Movement"] = "asset"`.

5. **No ERPNext core modification** — Canonical relationship remains ERPNext’s native Movement / `asset` filter.

### Scope

| Area | Change |
|------|--------|
| `asset_request_settings.json` | Default `prevent_duplicate_active_requests` = 0 |
| `asset_dashboard.py` | Dedupe AM + restore `asset` filter |
| Patch v558 | Remove known-bad custom DocType Link |
| Tests | Dashboard, patch idempotency, duplicate policy, Issue-from-Pool |
| Playwright | Asset Connections + duplicate request submit |

### Non-goals

- Do not remove the Prevent Duplicate Active Requests setting
- Do not delete Equipment Profile or other legitimate Asset custom links
- Do not change Asset Request fulfillment / Issue-from-Pool creation logic
- Do not modify ERPNext core `asset_dashboard.py`

### Tests

- `test_asset_dashboard_v558` / `test_asset_dashboard` / `test_asset_request` (duplicate policy)
- Playwright: `playwright_asset_connections_v558.mjs`
