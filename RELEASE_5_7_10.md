# erpnext_extensions 5.7.10

## Historical Rebuild campaign-scoped REST API

### Problem

Limited Historical Rebuild on Production required Bench/`frappe.call`
in-process entry points. Campaign create / sequential run methods were **not**
`@frappe.whitelist()` and returned HTTP 403 over REST. Operators and agents
could not run an allowlisted, checksummed, fail-fast campaign through HTTPS
token auth without local helper scripts or unsafe substitutes
(`repost_selected_api`, site-wide `execute_repost_item_valuation`).

### Audit (reuse, no duplicate engine)

| Capability | Existing | REST before | Work in 5.7.10 |
|------------|----------|-------------|----------------|
| Auth | Frappe token + Historical Repair roles | Yes | Reuse |
| Environment status | `historical_stock.api.session_info_api` | Partial | New `get_rebuild_api_status` |
| Voucher inspection | `frappe.client` + stock APIs | Indirect | New `inspect_vouchers` |
| Dependency discovery | `scoped_campaign.expand_*` / `build_scoped_manifest` | No | New `discover_dependencies` |
| Exact campaign create | `create_exact_voucher_campaign` | **403** | Whitelist wrapper + allowlist/checksum guards |
| Manifest checksum | `discovery.manifest_checksum` | No | Exposed via discover/create/preflight |
| Campaign preview | `campaign.status` | No | `preflight_campaign` / `get_campaign_status` |
| Sequential start | `run_sequential` | **403** | Async enqueue wrapper |
| Progress monitoring | `campaign.status` | No | `get_campaign_status` |
| Safe stop | `request_sequential_stop` | **403** | `request_campaign_stop` |
| Post validation | `voucher_integrity` + GL | Partial | `validate_campaign_results` |

**Intentionally not exposed:** `prepare_clean_start` / `prepare_sequential_rebuild`
(deletes all RIVs), `execute_repost_item_valuation`, full-company rebuild.

### New module

`erpnext_extensions.iran_accounting.historical_rebuild.api`

| Method | HTTP | Mutates |
|--------|------|---------|
| `get_rebuild_api_status` | GET/POST | No |
| `inspect_vouchers` | GET/POST | No |
| `discover_dependencies` | GET/POST | No |
| `create_exact_campaign` | POST | Yes (new campaign only) |
| `preflight_campaign` | GET/POST | No |
| `start_sequential_campaign` | POST | Yes (enqueue sequential) |
| `get_campaign_status` | GET/POST | No |
| `request_campaign_stop` | POST | Stop flag only |
| `validate_campaign_results` | GET/POST | No |

Permissions: read endpoints → Historical Repair read roles; mutate → System
Manager / Administrator (`require_repair`), same model as `historical_stock`.

### Scope enforcement

- Explicit voucher allowlist (max 200).
- Exact manifest = listed vouchers only (no silent identity expansion on create).
- Optional / required REST allowlist must be a subset of seed-voucher identities.
- `expected_checksum` must match `manifest_checksum` on create and again on start.
- Confirm tokens: `CREATE_EXACT_CAMPAIGN`, `START_SEQUENTIAL`.
- Does not resume or modify HRC-00009 / HRC-00010 implicitly.
- `stop_on_failure=True` by default; sequential runner unchanged.
- Start is asynchronous (`frappe.enqueue` long queue) by default.

### Accounting controls preserved

Integer IRR, `ROUND_HALF_UP`, SED amount authority, transfer inward conservation,
Case D post-SABE, strict validators — unchanged. API does not bypass ledger
contracts or invent SA/JE/Round Off.

### Migration

None. No DocType schema change. Scope JSON path unchanged under
`private/files/hrc_scoped_campaigns/`.

### Tests

`test_historical_rebuild_rest_api_v5710.py`

### First Production use (after deploy) — six Manufacture SEs

Seeds: `MAT-STE-2026-40332`, `40149`, `36826`, `36928`, `40902`, `43589`  
Company: `اسپاد فارمد دارو`

1. `get_rebuild_api_status`
2. `inspect_vouchers`
3. `discover_dependencies` (`mode=exact`)
4. Operator approves allowlist + checksum
5. `create_exact_campaign` (`confirm=CREATE_EXACT_CAMPAIGN`)
6. `preflight_campaign`
7. Operator approves execution
8. `start_sequential_campaign` (`confirm=START_SEQUENTIAL`)
9. Poll `get_campaign_status`
10. `validate_campaign_results`

Do **not** resume prior campaigns. Do **not** assume SA net residuals must become zero.

### Example REST paths

```text
POST /api/method/erpnext_extensions.iran_accounting.historical_rebuild.api.get_rebuild_api_status
POST /api/method/erpnext_extensions.iran_accounting.historical_rebuild.api.discover_dependencies
POST /api/method/erpnext_extensions.iran_accounting.historical_rebuild.api.create_exact_campaign
POST /api/method/erpnext_extensions.iran_accounting.historical_rebuild.api.start_sequential_campaign
```

Authorization: `token <API_KEY>:<API_SECRET>`

### Version

`erpnext_extensions` → **5.7.10**
