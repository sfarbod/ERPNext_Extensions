# Release 5.3.32 — Asset Depreciation Repair Campaign timeout / performance fix

## Summary

Fixes Production campaign stall caused by RQ `process_campaign_chunk`
timeouts (1800s) on heavy Assets and the resulting feeder death.

**No accounting repair semantic change.** Per-Asset repair still uses:

`reset_and_rebuild_asset` → standard `JournalEntry.cancel()` → VAD reconcile →
ADS rebuild/replace → G0–G12 gates → **one Asset = one commit**.

## Root cause (confirmed on Development + Production evidence)

1. Chunk of up to **20** Assets shared one RQ job with `timeout=1800`.
2. Dominant cost is **per-JE** `je.cancel()` (not ADS normalize).
3. Cancel path full-scanned:
   - `tabDepreciation Schedule.journal_entry` (~165k–184k rows, **no index**)
   - `tabStock Entry` consignment JE custom fields (**no index**)
4. Under Production concurrency, heavy Assets took ~1–4 min; chunks timed out;
   `enqueue_next_chunks` never ran → Pending work + zero live jobs (stalled
   “Running” campaign).
5. `tabSeries` 1020 on `ACC-GLE-` / `ACC-ADS-` is a **secondary** concurrency
   effect, not the primary timeout clock.

## Changes

| Piece | Change |
|-------|--------|
| Indexes (migrate patch) | `Depreciation Schedule.journal_entry`; Stock Entry consignment recognition/settlement JE fields |
| Chunk packing | Default `chunk_size=3` (clamp 1–8) + `max_jes_per_chunk=150` weighted packing |
| Feeder liveness | `process_campaign_chunk` always attempts feed in `finally`; `ensure_campaign_liveness` API; cron `*/5 * * * *` heartbeat |
| Abandoned Processing | Recover when RQ job for chunk is gone (grace 90s) or age ≥ 25 min; never while job is started/queued |
| Job timeout field | `job_timeout_seconds` (default 1800; secondary defense only) |
| Count refresh | Retry on MariaDB 1020 / transient errors |
| Defaults patch | Existing campaigns with unsafe `chunk_size` get v5.3.32 defaults |

### New / updated APIs

- `ensure_campaign_liveness(campaign)` — recover + feed (idempotent)
- `create_campaign(..., max_jes_per_chunk=, job_timeout_seconds=)`
- Scheduler: `ensure_all_running_campaigns_live` every 5 minutes

## Measured Development impact

### Index EXPLAIN (after migrate)

| Query | Plan | Rows |
|-------|------|------|
| `Depreciation Schedule` WHERE `journal_entry=?` | **ref** / `idx_journal_entry` | 1 |
| Stock Entry recognition JE | **ref** | 1 |
| Stock Entry settlement JE | **ref** | 1 |

Prior (no index): ALL / ~165k rows examined.

### Per-Asset cancel timing (post-index, Dev)

| Asset class | Asset | JEs | GL open | Cancel | Total | Gates |
|-------------|-------|-----|---------|--------|-------|-------|
| Light | 2442 | 45 | 90 | 6.5s | 6.9s | PASS |
| Medium | 1654 | 85 | 170 | 7.0s | 7.2s | PASS |
| Heavy | 3651 | 120 | 240 | 10.1s | 10.2s | PASS |

Earlier pre-index Dev sample (same class): Heavy 120 JE cancel ~30s → ~11s (~2.7×).

### Real RQ load tests (not `run_inline`)

**1 concurrent chunk** (`max_inflight=1`, campaign `AUD-ADRC-2026-00047`):
15 Assets (5×~45, 5×~85, 5×~120 JEs). Result: **14 Success**, 1 Manual Review
(`NEGATIVE_FINAL_BALANCE` — correct gate). **1205 JEs cancelled**. Timeouts: **0**.
tabSeries 1020: **0**. Asset runtime avg 8.2s / p95 11s / max 12s. RQ job durations
all ≤ ~14s (margin vs 1800: **>100×**). Feeder liveness recovered a mid-run stall
via `ensure_campaign_liveness`. Idempotent re-analyze: je_count=0, frac=0.

**2 concurrent chunks** (`max_inflight=2`, campaign `AUD-ADRC-2026-00049`):
15 Assets. Result: **14 Success**, 1 Failed (`RETRY_EXHAUSTED` on tabSeries **1020**).
**1190 JEs cancelled**. Timeouts: **0**. Error Log 1020-class: **12**. Asset p95 10s /
max 11s. RQ job p95 ~12s / max ~16.5s. **Conclusion:** 2-way concurrency improves
wall slightly but introduces naming-series 1020; prefer starting Production at
`max_inflight_chunks=1`–`2` and raise only if 1020 stays near zero.

With chunk ≤3 and ≤150 JEs, p95 RQ job duration stays well under 1800s.

## Not changed

- JE identity / `JournalEntry.cancel()` lifecycle
- VAD / ADS / Iran whole-IRR / G0–G12
- No `make_depreciation_entry` / no rebook
- Does **not** auto-enable `depreciation.post_depreciation_entries`

## Production resume (existing campaign `AUD-ADRC-2026-00002`)

Do **not** create a new campaign. After deploy+migrate of 5.3.32:

Current state to recover in-place:
Success 100 / Retryable 17 / Failed 20 / Pending 2275 / Queued 37 / Processing 7 abandoned.

1. Confirm indexes exist (`SHOW INDEX ... idx_journal_entry` etc.).
2. Confirm no live `Asset Depr Repair AUD-ADRC-2026-00002-*` RQ jobs.
3. Confirm campaign fields: `chunk_size≤8`, `max_jes_per_chunk=150`, prefer
   `max_inflight_chunks=2` initially (or 1 if 1020 storms).
4. Call `ensure_campaign_liveness` (or wait ≤5 min for cron).
5. Abandoned Processing → Retryable Failed; Queued rows without jobs are
   reclaimed by feeder; Pending continues; Retryable re-enters weighted packing.
6. Keep depreciation scheduler **stopped=1**.
7. Canary: watch first 10–20 Success after resume; stop if G-gate failures or
   timeout storm.
8. Do not raise workers to 3 until 1020 rate is clean at 2.

## Deploy

1. Deploy **5.3.32**
2. `bench migrate` (indexes + DocType fields + defaults patch)
3. Restart workers / reload scheduler events
4. Resume plan above — do not raise workers first
