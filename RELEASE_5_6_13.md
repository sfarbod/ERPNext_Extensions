# erpnext_extensions 5.6.13

## HRC-00004 — late Completed RIV after retry exhaustion + I5 investigation

### Production symptoms (post-5.6.12)
Session `20261024233258` on `HRC-00004` recorded **Failed** while RIVs later showed
**Completed** (`9hhpnnvkne`, `d3829ku65b`; earlier `p432aoq6v9`). Some error_logs
still contain MariaDB 1020 / `retry_attempts_exhausted`. A separate I5 mention
points at Manufacture `MAT-STE-2026-25106` / detail `hcg8d01s31` / item `30200016`.

Those later RIVs / session JSON **are not** in morning backup `20261024_085950`.

### Why 5.6.12 was insufficient
5.6.12 reconciled Completed **inside** the retry loop (after each exception or
before the next attempt). If all **5** attempts raised 1020 while Core still
held the RIV mid-`update_data_in_repost` (progress `db_set` + mid-loop commit),
the executor returned `attempts_exhausted` **without a post-loop re-read**.
Core then committed Completed after the executor had already marked Session Failed.

`error_log` on a Completed RIV may be a **historical** 1020 — not current failure.

### Root cause of 1020
ERPNext Core `stock_ledger.update_entries_after.update_data_in_repost`:

- `repost_doc.db_set({total_vouchers, vouchers_posted})`
- then `frappe.db.commit()` mid-repost (outside tests)

Competing writers on `tabRepost Item Valuation` (progress snapshots, realtime
publish, another worker touching the same row, or CHECKREAD after a prior
committed progress update) raise `QueryDeadlockError(1020)`. This is transient
concurrency on the **progress row**, not SLE/GL corruption by itself.

Increasing retry count alone does not remove the Core mid-commit race.

### Fix (5.6.13)
`final_completion_barrier` after the last transient attempt / loop exhaust:

1. Rollback already done; wait **outside** the transaction (default 120s × 1 grace).
2. Re-read RIV (and do not create a duplicate).
3. If Completed → SLE-chain verify → advance identity **once**.
4. If In Progress → stay in grace wait.
5. If still Failed / dirty chain / I5 → fail closed; preserve error history.

Configurable: `hrc_seq_riv_post_exhaust_grace_waits` (default 1).

I5 / AllocationError remain **non-retryable**.

### I5 / `MAT-STE-2026-25106` on restored morning backup
**Not reproduced.** Restored state:

- Purpose Manufacture, submitted, additional cost 0.
- FG `30200016` qty 123 @ warehouse Quarantine semi-FG; amount **1,228,890,294**
  (rate 9,990,978) — differs from later Production snapshot (9,710,951 /
  1,194,446,973) so evening HRC mutation is missing.
- Second incoming reject row qty 2 amount 0.
- **No outgoing (source) rows** → I5 pool check: `other_incoming=0`, assert **PASS**.

I5 on later Production is **state-dependent** (RIV-revalued scrap/secondary
incoming vs consumed pool). Fail-closed path is tested via injection; **not
claimed fixed**. Do not bypass I5.

1020 race and I5 are **independent** (progress-row concurrency vs manufacture
pool invariant).

### Tests
- `test_sequential_riv_retry_v5612` — exhaustion + late Completed via barrier;
  I5 fail-closed; prior 5.6.12 cases; Two-Slice / SABE no-regression.
- Morning-backup I5 historical assert PASS (documented limitation).

### Production recovery (operator-only)
Campaign `HRC-00004`, session `20261024233258` (or current session if it moved):

1. READ-ONLY: confirm Manifest checksum
   `3b3d15538521362716d9a20b11215b4c7bb23feabfd8b7a5f1f2bd18691a6334`.
2. For each listed RIV (`9hhpnnvkne`, `d3829ku65b`, `p432aoq6v9`, `5s61kqov83`):
   read **current** status + `modified` vs `error_log` timestamps.
3. If Completed: run SLE-chain / I5 inspect for that identity — do not trust
   status alone.
4. Reconcile session: add identity to `identities_done` once if verified;
   move RIV from active `riv_failed` to `riv_failed_history`; keep error_log.
5. If any RIV Completed but I5 dirty → leave Failed; do not resume that identity.
6. Do not reset campaign, delete RIVs, or force status.
7. Restore session JSON if Redis stale (`restore_session_from_file`).
8. Clear executor lock only if no live executor; then resume sequential.
9. `MAX_ACTIVE_RIV=1`. Do not auto-resume from Development.

### Migration
No schema change. Version **5.6.13**. Frappe 16.52.0 / ERPNext 16.51.1 / HRMS 16.51.0.
