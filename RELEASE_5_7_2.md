# erpnext_extensions 5.7.2

## Sequential rebuild Pause/Resume reliability (HRC-00009)

### Incident (Production forensic, Development fix only)

- Campaign `HRC-00009`, session `20261106014343`, RIV `611vj32kg4`
- Sequential executor Paused at 11:00:18 on `in_progress_unresolved` after the
  In-Progress wait barrier (~30 minutes under prior defaults)
- The same RIV Completed at 11:08:03 (~61 minutes wall)
- Resume risk: sticky `current_riv` could create another RIV / double-count
  progress without reconciliation
- Executor lock was not heartbeated during resilient waits
- Completed RIV retained prior-attempt `error_log` (Budget + Deadlock) without
  archive/banner, inviting terminal-status misread

### Fixes

1. **Resume reconciliation** — `reconcile_session_current_riv` runs before any
   new RIV create (`execute_one_identity` / `run_sequential`). Completed current
   RIV is reconciled exactly once via `mark_identity_done_once`; In Progress /
   Queued current RIV is reattached without create.
2. **Configurable long-running barrier** — default
   `SEQ_IN_PROGRESS_MAX_WAITS` raised 15 → **45** (~90 minutes after grace).
   Site config `hrc_seq_riv_in_progress_max_waits` clamped to
   `[0, SEQ_IN_PROGRESS_MAX_WAITS_CAP]` (cap **120** ≈ 4 hours). Controlled
   Pause retained; no unbounded wait.
3. **Executor lock + RIV claim heartbeat** — `wait_for_retry_delay` accepts
   `heartbeat`; sequential ownership refresh runs during In-Progress waits
   (claim + campaign executor lock, ownership verified before refresh).
4. **Scheduler ownership fail-closed** — `riv_execute_guard` defers execute /
   enqueue when ownership probe errors (no fail-open dual-exec with Sequential).
5. **Error-log semantics** — on Completed reconcile, prior-attempt `error_log`
   is copied to session `riv_error_archive` and bannered
   (`[HISTORICAL RETRY ERRORS — ARCHIVED…]`); original traceback preserved;
   final status remains Completed.

### Configuration (optional site_config)

| Key | Default | Notes |
|-----|---------|-------|
| `hrc_seq_riv_in_progress_max_waits` | `45` | Clamped 0–120 |
| `hrc_seq_riv_retry_delay_seconds` | `120` | Unchanged |
| `hrc_seq_riv_retry_max_attempts` | `5` | Unchanged |
| `hrc_seq_riv_post_exhaust_grace_waits` | `1` | Unchanged |

No DocType migration. No stock / SLE / GL / Bin changes.

### Tests

`test_sequential_pause_resume_v572.py` — long In-Progress → Pause → resume
reconcile; no duplicate counters; lock heartbeat; second executor refused;
scheduler defer; Deadlock then success; non-retryable fail-closed; stop;
error-log archive.

### Production recommendation

**Safe for controlled Production deploy of the app code only.** Do **not**
auto-resume `HRC-00009` as part of deploy. After deploy: confirm version
`5.7.2`, optionally set `hrc_seq_riv_in_progress_max_waits` (e.g. 60), then
operator-authorized resume of the paused session so resume reconcile can absorb
`611vj32kg4` before any new create.

### Version

`erpnext_extensions.__version__` → **5.7.2**
