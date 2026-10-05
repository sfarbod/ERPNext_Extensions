# erpnext_extensions 5.5.4

## Job Card Stock Rebuild — Atomic Queued Apply Manufacture Repair

### Summary

1. **Apply Manufacture Repair now runs asynchronously on the `long` queue** — Desk Start API returns in milliseconds; the browser no longer waits for valuation / commit over HTTP.

2. **Atomic transaction preserved** — ONE RQ job · ONE Frappe DB connection · ONE business transaction · FULL verification · ONE final commit. Pre-commit failure → ROLLBACK ALL.

3. **No intermediate business commits** — Progress/status live in Redis (`frappe.cache()`) outside the business SQL transaction. Progress writes never call `frappe.db.commit()`.

4. **Shared single-flight for Dry Run and Apply** — Lock key `jcsr:repair:lock:<job_card>` blocks Dry+Dry, Apply+Apply, Dry+Apply, and Apply+Dry. Concurrent starts reconnect to the same `run_id`.

5. **Refresh / reconnect** — `localStorage` + `get_active_manufacture_repair_apply` / shared active-repair API resume polling after reload. Browser lifetime does not affect worker transaction lifetime.

6. **Stale-plan protection** — Worker revalidates fingerprint before mutation; mismatch → `STALE_PLAN` with `mutated=false` / `committed=false`.

7. **Final UI status** — Successful Apply reports **APPLY PASS — COMMITTED** (`status=COMMITTED`, `mutated=true`, `committed=true`). Never shows PASS before commit.

8. **Post-commit status recovery** — If DB commit succeeds but Redis status update fails, committed evidence is persisted (`jcsr:repair:committed:<job_card>`). Recovery never claims rollback and blocks unsafe duplicate Apply.

9. **Worker timeout 900s** — Staging Dry Run valuation has been observed >6 minutes for PO-JOB08760; 600s left insufficient margin. Lock TTL 25 min; stale detection 20 min.

10. **Accounting contract unchanged** — `MANUFACTURE_COSTING_CONTRACT_VERSION = 5.3.43`. No Golden Rule / scrap / reject / logistics / bridge semantic change.

11. **Known performance limitation** — Valuation remains expensive due to Core `update_entries_after` N+1 SQL (~180k queries / 34 roots on this fixture). **N+1 optimization intentionally deferred** (not in 5.5.4).

12. **SAVEPOINT fix preserved** — `bc8c452` shared-logistics scan transaction safety remains; SP01–SP10 still PASS.

### Architecture

| Action | Execution |
|--------|-----------|
| Scan | Synchronous HTTP |
| Dry Run | One Frappe `long` queue job → poll status → ROLLBACK |
| Apply | One Frappe `long` queue job → poll status → ONE FINAL COMMIT |

Invariant for Apply:

`ONE JOB · ONE CONNECTION · ONE BUSINESS TRANSACTION · VERIFY · ONE FINAL COMMIT`

### APIs

- `start_manufacture_repair_apply(job_card, plan, confirm=1)` → `{run_id, job_id, status=QUEUED, mode=APPLY, ...}`
- `get_manufacture_repair_apply_status(run_id)` → lightweight poll payload
- `get_active_manufacture_repair_apply(job_card)` → reconnect helper
- Dry Run APIs unchanged in shape; shared lock/infrastructure via `queued_repair`
- Legacy synchronous `apply_manufacture_repair` kept for engine/tests; Desk UI must use Start Apply

### Version

- `erpnext_extensions`: **5.5.4**
- `MANUFACTURE_COSTING_CONTRACT_VERSION`: **5.3.43** (unchanged)
