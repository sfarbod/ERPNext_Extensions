# Release 5.1.7 — Material Request Connections / Asset Request link

## Scope

Hotfix only: Material Request form Connections raised

`MySQLdb.OperationalError: (1054, "Unknown column 'custom_asset_request' in 'WHERE'")`

No Asset Request workflow, fulfillment, dimension, or schema changes.

## Root cause

`override_doctype_dashboards["Material Request"]` mapped:

```text
non_standard_fieldnames["Asset Request"] = "custom_asset_request"
```

Frappe `get_open_count` treats that as an **external** reverse link and runs:

```sql
SELECT … FROM `tabAsset Request` WHERE `custom_asset_request` = '<Material Request name>'
```

`custom_asset_request` exists only on **Material Request** (→ Asset Request.name).
Asset Request has no such column.

## Fix

Use the Frappe-native **internal** link on Material Request:

```text
internal_links["Asset Request"] = "custom_asset_request"
```

and remove the invalid `non_standard_fieldnames` entry.

Asset Request → Material Request remains:

```text
non_standard_fieldnames["Material Request"] = "custom_asset_request"
```

Generated MRs still stamp `Material Request.custom_asset_request = Asset Request.name`.

## Migration

Not required. Dashboard override is Python-only; no patch / schema change.

## Version

App version **5.1.7**.
