# erpnext_extensions 5.7.56

## Fix — Account Rename / User Settings Data Too Long

### Summary

Version **5.7.56** fixes `MySQLdb.DataError: (1406, "Data too long for column
'data' at row 1")` when updating an Account Number (Frappe rename → User
Settings sync). The failure was observed on production Account
`511101 - سربار - E` → number `211237`.

### Root cause

1. Frappe 16.54.0 stores desk preferences in `__UserSettings.data` as **TEXT**
   (max **65,535** bytes).
2. Account Explorer (this app) persists Grid preferences plus Workspace share
   tokens in the same blob. Several users already sit at ~64–65 KB.
3. `update_account_number` → `frappe.rename_doc` → `update_user_settings` →
   `sync_user_settings()` flushes **all** Redis `_user_settings` entries into
   `__UserSettings`. Any oversize Account Explorer row raises MySQL 1406 and
   aborts the rename transaction — including filter rewrites for the Account
   DocType itself.

This is an upstream Frappe column-capacity limitation amplified by legitimate
erpnext_extensions Account Explorer preferences — not a silent corruption of
rename logic, and not fixed by truncating JSON.

### Fix

| Layer | Change |
|---|---|
| Schema | Idempotent `ALTER TABLE __UserSettings MODIFY data MEDIUMTEXT` |
| Runtime | Monkey-patch `sync_user_settings` (also rebound on `rename_doc`) to ensure MEDIUMTEXT before flush, and FIFO-compact Account Explorer `Workspace.tokens` only when over a soft 60 KB budget |
| Runtime | Monkey-patch `rename_doc.update_user_settings` so saved Linked filters match with `LIKE %old%` (Frappe 16.54.0 used `LIKE 'old'` without wildcards, which never matched JSON) |
| AE client | Token limit 24 → **8**; add **48 KB** byte-budget prune (preserves Grid / unrelated keys) |
| Migrate | Patch `expand_user_settings_data_mediumtext_v5756` + `after_migrate` ensure |

Preserved:

- Valid Grid preferences and unrelated User Settings keys
- Normal Frappe rename lifecycle / transaction integrity
- Non-Account DocType renames (same safer sync path; no behavioral change when under limit)
- Account naming / numbering rules

Not done:

- No Frappe / ERPNext core edits
- No broad exception swallow of DataError
- No raw-SQL bypass of rename
- No truncation of arbitrary User Settings JSON

### Schema justification

| Item | Detail |
|---|---|
| Change | `TEXT` → `MEDIUMTEXT` on `__UserSettings.data` |
| Why | Prefer preserving preferences over shrinking AE token history mid-rename |
| Compatibility | Reads/writes unchanged; MEDIUMTEXT is a strict capacity superset |
| Rollback | If every `data` value ≤ 65,535 bytes, `MODIFY data TEXT` is safe; otherwise keep MEDIUMTEXT |
| Impact | Online DDL on an internal Frappe table; no DocType metadata |

### Tests

`erpnext_extensions/tests/test_account_rename_user_settings_v5756.py`:

1. MEDIUMTEXT patch idempotent
2. AE compact preserves Grid / unrelated keys
3. Oversized sync fails on TEXT, succeeds after compat
4. Account rename with normal User Settings + filter rewrite
5. Rename with extensive AE prefs across multiple users
6. Repeated renames without duplicate List filters
7. Large serialized settings remain valid JSON
8. Simulated sync failure rolls back Account number change

### Compatibility

- Frappe 16.54.0 / ERPNext 16.53.0
- Isolated to User Settings sync + Account Explorer token budget
- No change to Account autoname algorithm

### Safety

- Development only for this change set.
- No Production deploy, push, or tag from this release commit.
