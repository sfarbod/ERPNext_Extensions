# erpnext_extensions 5.6.28

## Sequential RIV Execution Ownership (HRC-00008)

### Summary
Prevents Core parallel/RQ reposting from dual-executing a RIV that the
Sequential Historical Rebuild runner is already executing synchronously.
Also avoids marking a Sequential Session `Failed` while the RIV is still
`In Progress` (the Completed-after-Failed divergence seen on HRC-00008).

**Development only in this change set. No push / publish / production deploy.**

---

### Root cause (architecture)
Sequential `execute_one_identity` calls ERPNext `execute_reposting_entry`
**in-process**. Core `get_entries_with_active_jobs()` only inspects **RQ** job
IDs. With `enable_parallel_reposting=1` and the 15-minute
`run_parallel_reposting` cron, Core can enqueue the same Queued/In Progress
RIV. Concurrent `db_set` on `tabRepost Item Valuation` under
`innodb_snapshot_isolation=ON` yields MariaDB **1020** /
`QueryDeadlockError`. Core treats that as recoverable (`In Progress`).
Sequential retry barriers could exhaust and mark Session **Failed** while a
surviving executor later sets the RIV **Completed**.

### Architecture decision
**Keep Synchronous Sequential execution** (deterministic one-identity control)
and add **shared Redis ownership** visible to Core enqueue/active-job paths.
Full migration to RQ-only Sequential was rejected: it conflicts with
`_empty_rq()`, `continue_reposting` chaining, and campaign fail-fast control,
without fixing ownership by itself.

### Fix
1. `claim_riv_execution` / atomic Lua refresh / token-safe release + active-set
   registry (`db_concurrency.py`). Default claim TTL = **6 hours**.
2. Sequential records created RIV, claims **before** DB commit, sets
   `frappe.local.iran_hr_riv_exec_owner`, **asserts ownership before each
   execute**, releases in `finally`. Claim conflict → Pause (not Fail).
3. `riv_execute_guard` patches:
   - `enqueue_reposting_entry` — skip when claim held
   - `get_entries_with_active_jobs` — union RQ actives with claimed RIVs
   - execute wrappers — defer when foreign claim held
4. Final barrier: extended In Progress polling; unresolved / ownership-lost →
   Session **Paused** (not Failed).
5. `RivExecutionOwnershipError` on lost claim → Pause path in resilient retry.

### Known limitations (documented, not silent)
- Redis and MariaDB are **not** one atomic unit. Ordering is claim-then-commit;
  claim failure after create commits the Queued RIV and Pauses for reconcile.
- Ownership is re-asserted **between** Sequential execute attempts and waits.
  A **single** in-flight Core `repost()` that outlives claim TTL without a
  mid-repost refresh could theoretically race; mitigated by 6h TTL (≫ observed
  ~15m RIVs). Not a Core fork of `update_data_in_repost`.
- Enqueue/execute guards **fail-open** if Redis probe throws (preserve unrelated
  Core reposting). Sequential **fails closed** on claim acquire / assert.
- No live multi-worker soak against production RIV `jr2kvnnc64` in this release.

### Not changed
- No ERPNext core edits.
- No automatic resume of HRC-00008.
- No SLE/GL mutations as part of this release.
- Manifest checksum / completed identities preserved by design.

### Tests
- `test_sequential_riv_ownership_v5628` (ownership, concurrent claim, TTL loss,
  Pause on ownership lost)
- Existing sequential retry / 1020 / guard / campaign status suites

### Version
`erpnext_extensions.__version__` → **5.6.28**
