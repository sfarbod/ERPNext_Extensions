# erpnext_extensions 5.7.55

## Costing — Legacy Persian Workspace Removal & Account Control Enhancement

### Summary

Version **5.7.55** permanently removes the obsolete Persian Costing Desk module
(`بهای تمام شده`) and adds an optional **Account Control** Link field on
Expense Account Dimension Rules. Account Control is a reporting/classification
dimension only — it does **not** change GL matching or allocation math.

### 1. Legacy Persian Costing Desk cleanup

Idempotent patch `remove_legacy_persian_costing_desk_v5755`:

| Record | Action |
|---|---|
| Desktop Icon `بهای تمام شده` | Deleted when present |
| Sidebar `بهای تمام شده` | Deleted when present |
| Workspace / Workspace Sidebar Persian | Deleted when present |
| Module Def Persian | Deleted only if no DocTypes remain |
| English `Costing` Workspace / Icon / Sidebar / Module | **Preserved** |

Persian workspace/sidebar fixture JSON under `costing/workspace/bahaaye_tamaam_shode`
and `costing/sidebar/بهای_تمام_شده` were removed so migrate cannot reimport them.

### 2. Account Control field

| Property | Value |
|---|---|
| DocType | Expense Account Dimension Rule |
| Fieldname | `account_control` |
| Type | Link → Account |
| Required | No |
| Default | Empty |
| In List View / Standard Filter | Yes |

Companion read-only `account_control_name` (fetch from Account).

Existing ~3,026 rules keep blank Account Control. Values are never auto-filled
from Account, parent account, or hierarchy.

Company validation rejects Account Control accounts that do not belong to the
Settings Company. Invalid Account references are rejected.

### 3. Propagation (new calculations only)

On Monthly FG Cost Calculation rebuild:

| DocType | Account Control source |
|---|---|
| MFG Cost Expense Pool | Matching Expense Account Dimension Rule |
| MFG Cost GL Line | Parent pool / matching rule |
| MFG Cost Batch Expense Allocation | Parent pool |
| MFG Cost Account Dimension Summary | Batch × Expense rows |
| MFG Cost Product Expense Allocation | Schema ready; new revisions still skip PxE writes |

Historical calculation snapshots are **not** rewritten. Blank Account Control on
historical detail rows is expected until a period is recalculated.

### 4. Filters and exports

Account Control is `in_standard_filter` on Expense Rules (child), Expense Pool,
GL Line, Batch Expense Allocation, and Account / Dimension Summary.

Server-side Costing CSV/XLSX export (`export_costing.py`) includes all meta
value fields — Account Control code and name are exported automatically and
respect filters / full result sets.

### 5. Migration

| Patch | Purpose |
|---|---|
| `remove_legacy_persian_costing_desk_v5755` | Persian Desk cleanup |
| `add_account_control_costing_v5755` | Reload DocTypes; verify blank init |

Both are idempotent. Historical patch module names are unchanged.

### 6. Tests

`test_costing_account_control_v5755.py` covers:

- Field metadata (Link / optional / filters)
- GL matching ignores Account Control
- Pool → Batch Expense propagation (set and blank)
- Persian cleanup skip-missing / never-delete-English / double-run
- Existing rules remain blank and intact

Plus existing Costing optional-dimension and desk sidebar regressions.

### Known limitations

- Historical monthly detail rows are not backfilled with Account Control.
- Recalculation is required to stamp Account Control onto Expense Pools / GL /
  Batch Expense rows for a given period.
- Account Control is not part of the uniqueness / overlap key for rules.

### Safety

- Development only for this change set.
- No Production deploy, push, or tag from this release commit.
- No Costing transactional amounts or source GL/SLE masters modified.
