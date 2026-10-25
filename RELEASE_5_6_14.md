# erpnext_extensions 5.6.14

## Sequential rebuild — queue-aware waiting

### Symptom (Production)
```text
bench execute … campaign.run_sequential_rebuild --kwargs '{"campaign":"HRC-00004",…}'
ValidationError: Active RIV present (2); sequential policy forbids starting another
```
Location: `execute_one_identity → _assert_no_active_riv`.

Operator could not resume `HRC-00004` while unrelated or leftover Queued /
In Progress RIVs occupied the site, even though sequential policy only needs
**MAX_ACTIVE_RIV=1 for campaign-created work**.

### Root cause
Hard fail on any site-wide Queued/In Progress RIV. Temporary queue occupancy
aborted the executor instead of waiting for drain.

### Fix
Module `sequential_riv_queue_wait.py` + wiring in `sequential_rebuild.py`:

| Behavior | Detail |
| --- | --- |
| Detect | DB count of submitted RIV with status **Queued** or **In Progress** |
| Wait | Poll every **30s** (site_config `hrc_seq_riv_queue_poll_seconds`) |
| Max wait | None by default (wait until drain, STOP, or lock loss) |
| Transaction | `rollback`/`commit` before sleep — no open write txn while waiting |
| Lock | Keep single-executor campaign lock; heartbeat refresh TTL; stop if lost |
| Session | Status `Waiting for RIV Queue`; JSON+Redis via existing revision save |
| STOP | Graceful → Session/Campaign **Paused**; no new RIV |
| Recheck | Before every identity create; second check immediately pre-create |
| Safety | Still **MAX_ACTIVE_RIV=1** for sequential creates; I5 / integrity fail-closed |

Preserves v5.6.12–5.6.13: 1020/1205/1213 retry, 5×120s, `final_completion_barrier`,
late Completed reconcile, exactly-once identities, STOP, JSON revision.

### Remaining concurrency note
External ERPNext workers may still enqueue RIVs outside this campaign. The
executor rechecks the DB immediately before each create and refuses to create
while the queue is non-empty. It cannot globally serialize non-campaign RIV
creators without Core changes — strongest safe coordination under the
campaign executor lock.

### Tests
`test_sequential_riv_queue_wait_v5614` — zero/one/two active, drain 2→1→0,
extended wait, STOP, lock loss, heartbeat, wiring, I5 still non-retryable,
no-regression vs 5.6.12/5.6.13 retry barrier (fake clock, no real 30s sleeps).

### Production recovery (operator-only)
1. Install **5.6.14**.
2. Do **not** delete RIVs or clear locks while an executor is alive.
3. Re-run `run_sequential_rebuild` for `HRC-00004` — executor waits for queue
   drain, then continues from `identities_done`.
4. Preserve Manifest/checksum and audit history.

### Migration
No schema change. Version **5.6.14**. Frappe 16.52.0 / ERPNext 16.51.1.
