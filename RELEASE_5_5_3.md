# erpnext_extensions 5.5.3

## Job Card Stock Rebuild — Queued Manufacture Repair Dry Run + SAVEPOINT fix

### Summary

1. **Shared logistics SAVEPOINT ownership fix** (`bc8c452`) — nested full `frappe.db.rollback()` during cancel-probe no longer destroys `jc_shared_cancel_*` SAVEPOINTs. Probe ownership demotes nested full rollback to savepoint rollback, blocks commit, and uses collision-safe savepoint names. Scan stays synchronous.

2. **Dry Run moved to Frappe background queue** — Desk no longer waits on a 70–120s HTTP request. `start_manufacture_repair_dry_run` enqueues **one** `long`-queue job (timeout 600s) that runs the entire existing `run_repair(dry_run=True)` engine on one DB connection / one business transaction, then rolls back.

3. **Progress / status polling** — Redis-backed operational run state (TTL 24h) exposes status, coarse phase, progress %, optional valuation root counts. UI polls ~2s via `get_manufacture_repair_dry_run_status`.

4. **Single-flight protection** — Redis `SET NX` lock per Job Card (`jcsr:dry_run:lock:<job_card>`). Concurrent starts reconnect to the active `run_id`. Apply is blocked with `DRY_RUN_IN_PROGRESS` while a Dry Run is active.

5. **Browser refresh / reconnect** — `run_id` in `localStorage` plus `get_active_manufacture_repair_dry_run` resume polling after reload. Worker continues if the browser disconnects.

6. **Apply remains synchronous and unchanged** — not queued; same atomic commit-after-verify contract.

7. **Accounting contract remains 5.3.43** — no semantic change to Golden Rule, Component Scrap, Product Reject, Type A/B/C residuals, shared logistics, or temp bridge.

8. **Queue solves HTTP / nginx timeout exposure**, not underlying T17 valuation runtime. Valuation still runs synchronously inside the same worker transaction.

### Architecture

| Action | Execution |
|--------|-----------|
| Scan | Synchronous HTTP |
| Dry Run | One Frappe `long` queue job → poll status |
| Apply | Synchronous HTTP (unchanged) |

Invariant for Dry Run:

`ONE JOB · ONE CONNECTION · ONE BUSINESS TRANSACTION · ONE FINAL ROLLBACK`

Operational progress lives in Redis (outside the business SQL transaction). Business mutations never commit in Dry Run (`mutated=false`, `committed=false`).

### APIs

- `start_manufacture_repair_dry_run(job_card, plan)` → `{run_id, job_id, status=QUEUED, ...}`
- `get_manufacture_repair_dry_run_status(run_id)` → lightweight poll payload
- `get_active_manufacture_repair_dry_run(job_card)` → reconnect helper
- Legacy `dry_run_manufacture_repair` kept for direct/engine tests; Desk UI must use the queued start API

### Version

- `erpnext_extensions`: **5.5.3**
- `MANUFACTURE_COSTING_CONTRACT_VERSION`: **5.3.43** (unchanged)
