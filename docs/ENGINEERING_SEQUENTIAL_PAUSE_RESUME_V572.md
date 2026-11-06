# Engineering report — Sequential Pause/Resume reliability (v5.7.2)

**Scope:** Development only. Baseline: restored `development.localhost` +
`erpnext_extensions` 5.7.1. Did **not** resume/execute `HRC-00009`, enqueue any
RIV, modify Production, or touch stock/SLE/GL/Bin.

## 1. Root cause and code paths

| Factor | Path | Effect |
|--------|------|--------|
| Barrier too short | `sequential_riv_retry.SEQ_IN_PROGRESS_MAX_WAITS` (was 15 × 120s ≈ 30 min) via `final_completion_barrier` → `in_progress_unresolved` | Pause while RIV `611vj32kg4` still running (~61 min) |
| No resume reconcile | `execute_one_identity` / `run_sequential` created next RIV while sticky `sess.current_riv` already Completed | Counter skew (`riv_created` > `riv_completed`); risk of duplicate identity work |
| No lock heartbeat on waits | `wait_for_retry_delay` / `_save_seq_refresh` did not refresh campaign executor lock during In-Progress polls | Lock TTL could expire; second executor could start |
| Scheduler fail-open | `riv_execute_guard.execute_reposting_entry` / `enqueue_reposting_entry` swallowed ownership probe errors | Possible dual-exec window for session-owned RIV |
| Stale error_log | Core leaves last failed-attempt text on later-Completed RIV; no archive/banner | Desk readers can misread Completed as terminal failure |

**Confirmed timeline:** Pause 2026-11-06 11:00:18 (`in_progress_unresolved`);
RIV Completed 11:08:03. Extra RIVs 10:26–10:45 are consistent with create-before-
durable claim / scheduler visibility races addressed by claim-before-commit
(v5.6.28) + fail-closed guard (this release).

## 2. Minimal implementation

- `sequential_riv_retry.py` — barrier defaults/cap; heartbeat on waits;
  `archive_completed_riv_attempt_errors`; `reconcile_session_current_riv`;
  `mark_identity_done_once` does not inflate `riv_completed` when identity
  already done; heartbeat threaded through resilient execution / barrier.
- `sequential_rebuild.py` — resume reconcile before create; reattach helper;
  ownership heartbeat (RIV claim + executor lock) during resilient waits;
  resume gate at `run_sequential` start.
- `riv_execute_guard.py` — fail-closed defer/skip on ownership probe errors.
- `db_concurrency.py` — claim token tolerates unbound site (tests / edge).

## 3. Regression tests and results

File: `tests/test_sequential_pause_resume_v572.py`

Suite run (Development):

```
pytest test_sequential_pause_resume_v572.py \
       test_sequential_riv_retry_v5612.py \
       test_sequential_riv_ownership_v5628.py \
       test_sequential_riv_queue_wait_v5614.py
→ 70 passed
```

Covered: long In-Progress beyond original barrier → Pause → resume reconcile;
no duplicate counters; heartbeat; second executor refused; scheduler defer;
Deadlock then success; non-retryable stop; stop flag; error-log archive.

## 4. Concurrency and idempotency review

- **Campaign executor lock:** acquire refuses foreign session; refresh only if
  owned; heartbeat during waits.
- **RIV claim:** SET NX; refresh Lua token-match; assert before execute; release
  token-match only; guard defers foreign / probe-fail paths.
- **Progress:** `mark_identity_done_once` + resume reconcile clear `current_riv`
  after one advance; reattach never creates a second RIV for the same current.
- **Idempotent resume:** second `reconcile_session_current_riv` after clear is
  `skipped` / no counter change.

## 5. Migration / configuration

- **No DocType / schema migration.**
- Optional `site_config.json`: `hrc_seq_riv_in_progress_max_waits` (0–120,
  default 45).
- Deploy app code only; `bench migrate` not required for this change.

## 6. Production recommendation

**CONDITIONAL GO** for controlled Production **code deploy** of 5.7.2.

**Do not** auto-resume `HRC-00009` or enqueue RIVs as part of deploy.

After deploy: verify `__version__ == 5.7.2`, optionally raise
`hrc_seq_riv_in_progress_max_waits` for heavy identities, then
**operator-authorized** resume so reconcile absorbs Completed `611vj32kg4`
before any new create.

## 7. Changed files

- `erpnext_extensions/__init__.py`
- `erpnext_extensions/iran_accounting/historical_rebuild/sequential_riv_retry.py`
- `erpnext_extensions/iran_accounting/historical_rebuild/sequential_rebuild.py`
- `erpnext_extensions/iran_accounting/integration/riv_execute_guard.py`
- `erpnext_extensions/iran_accounting/historical_stock/db_concurrency.py`
- `erpnext_extensions/iran_accounting/tests/test_sequential_pause_resume_v572.py`
- `RELEASE_5_7_2.md`
- `docs/ENGINEERING_SEQUENTIAL_PAUSE_RESUME_V572.md`
