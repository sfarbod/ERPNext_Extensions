# erpnext_extensions 5.6.12

## Resilient sequential RIV retry & session reconciliation

### Production incident (`HRC-00004` / session `20261024220900`)
RIV `p432aoq6v9` (`13100018` @ packaging approved) raised:

`QueryDeadlockError: (1020, "Record has changed since last read in table 'tabRepost Item Valuation'")`

at `stock_ledger.update_data_in_repost → repost_doc.db_set`, while Core later marked the
same RIV **Completed**. The sequential executor recorded permanent campaign/session
**Failed** even though the identity work finished.

### Root-cause / race analysis
1. Core repost commits SLE/GL incrementally and updates RIV progress via `db_set`.
2. MariaDB 1020 (CHECKREAD) on that progress write is a **transient concurrency**
   signal — not a valuation integrity failure.
3. Short-backoff retry (v5.6.9) could exhaust or surface the exception while Core
   still completed the RIV on another write path.
4. Session then appended `riv_failed` and set Campaign=`Failed` without re-reading
   terminal RIV status → **false permanent failure**.

### Fix
Module `historical_rebuild/sequential_riv_retry.py` + wiring in `execute_one_identity`:

| Policy | Default |
| --- | --- |
| Retryable | MariaDB **1020 / 1205 / 1213** only |
| Max attempts | **5** (incl. first) |
| Delay | **120s** outside any open transaction |
| Non-retryable | I5 / AllocationError / validation — fail closed |
| MAX_ACTIVE_RIV | 1 |

State machine (per identity RIV):

1. Re-read RIV status (no duplicate create).
2. **Completed** → verify SLE chain → advance `identities_done` **once** (reconcile).
3. **In Progress** → wait 120s (STOP-aware) → recheck.
4. **Queued/Failed** + transient → rollback, wait 120s, retry same RIV.
5. After transient exception, **re-read**; if already Completed → reconcile (HRC-00004 race).
6. Exhausted / non-retryable / invalid Completed → fail closed; preserve Failed RIV audit.

Session hardening:

- JSON written **before** Redis; `revision` / `persisted_at`.
- Stale Redis cannot overwrite newer JSON.
- Session status `RetryWait` during delay (campaign stays Running until terminal).
- Operator STOP during wait → **Paused**, not Failed.
- Single executor lock per campaign unchanged.

Configurable via site_config:

- `hrc_seq_riv_retry_max_attempts` (default 5)
- `hrc_seq_riv_retry_delay_seconds` (default 120)

### Tests
`test_sequential_riv_retry_v5612` — injected failures + fake clock (no real 120s waits):
1020/1205/1213 success, 5× exhausted, Completed-while-waiting, In Progress wait,
already/not-yet counted, invalid Completed fail-closed, STOP mid-wait, dual executor
lock, non-retryable I5/AllocationError, no-regression Physical Two-Slice / SABE repair.

### Production recovery (operator-only — do not auto-run)
Campaign `HRC-00004`, session `20261024220900`, failed RIV `p432aoq6v9` (now Completed):

1. READ-ONLY verify RIV status=Completed and SLE chain for item/warehouse.
2. Confirm whether identity is already in `identities_done` (44).
3. If Completed + clean chain and not in done → reconcile once (add identity + riv to
   completed lists; move `p432aoq6v9` from active `riv_failed` to `riv_failed_history`).
4. If already in done → only clear stale `last_failure` / active failed pointer.
5. Preserve Manifest/checksum, all Completed RIVs, error_log history.
6. Clear STOP / stale executor lock **only if no live executor**.
7. `restore_session_from_file('20261024220900')` then `run_sequential` — skips done
   identities; does not recreate `p432aoq6v9` if reconciled.
8. MAX_ACTIVE_RIV=1; fail-fast on integrity remains.

### Migration
No schema migration. Version **5.6.12**. Compatible with Frappe 16.52.0 / ERPNext 16.51.1.
