# erpnext_extensions 5.7.50

## HRC Staggered Historical Reposting — Fast Bulk Queue + Conflict-Aware Dispatch

### Summary

Version **5.7.50** adds a staggered Historical Rebuild Campaign orchestration path
that separates **Stage A** (fast bulk Repost Item Valuation document creation)
from **Stage B** (conflict-aware dispatch onto ERPNext’s native `long` RQ queue).

Designed for Dev/restored sites to drain large historical RIV workloads without
weakening stock valuation rules or ERPNext’s native RIV execution path.

Sequential orchestration (`MAX_ACTIVE_RIV = 1`) remains unchanged and available.

### Architecture

| Stage | Queue | Responsibility |
|---|---|---|
| A — Bulk create | short/default (orchestration) | Create submitted RIV docs without waiting for completion |
| B — Dispatcher | `short` (or sync loop) | Interval + concurrency + conflict locks; enqueue only |
| Execution | `long` (ERPNext native) | `execute_reposting_entry(..., continue_reposting=False)` |

Conflict scopes (Redis, atomic multi-key acquire):

1. `item_code|warehouse` exclusive
2. `item_code` soft-cap (default exclusive)
3. Read-only future SLE voucher-key overlap probe before dispatch

### `dispatch_once` transaction safety

Each dispatcher tick commits (or rolls back) before reading RIV status so MariaDB
**REPEATABLE READ** snapshots cannot freeze `wait_capacity` on stale
In Progress/Queued rows after workers have completed RIVs.

### Configuration (`site_config` / `frappe.conf`)

| Key | Default |
|---|---|
| `hrc_stagger_dispatch_interval_seconds` | `5` |
| `hrc_stagger_max_global_concurrent_riv` | `1` |
| `hrc_stagger_max_concurrent_per_conflict_scope` | `1` |
| `hrc_stagger_retry_limit` | `3` |
| `hrc_stagger_lock_timeout_seconds` | `7200` |

### API

- `clone_campaign_manifest` (confirm=`CLONE_CAMPAIGN_MANIFEST`)
- `start_staggered_campaign` (confirm=`START_STAGGERED`)
- `get_staggered_status`
- `request_staggered_stop`

### Files

- `historical_rebuild/staggered_riv.py`
- `historical_rebuild/staggered_riv_locks.py`
- `historical_rebuild/staggered_riv_benchmark.py` (Dev-only harness)
- `historical_rebuild/api.py` / `api_service.py`
- `tests/test_hrc_staggered_riv_v5750.py`

### Safety

- Development / restored-backup sites only for clone and staggered runners
- Does not fork ERPNext RIV queue routing
- Does not alter stock valuation calculations
- Does not cancel/restart unrelated RIVs
- Does not suppress QueryDeadlockError / MariaDB 1020/1213
- Native Parallel Reposting must stay **OFF** while Stage B runs (no dual dispatcher)
- Do not count Core `Skipped` as covered unless Completed dependency evidence is verified

### Dev validation notes (non-blocking)

- Suite / subset: concurrency **2** observed MariaDB **1213** on some identity slices;
  concurrency **1** completed validation slices with zero deadlocks.
- Recommended production-safe starting concurrency: **1** until voucher-independence
  is proven for the target dataset.
- Runtime campaign artifacts (progress JSON, ledgers under `private/files`, logs)
  are **not** part of this release package.
