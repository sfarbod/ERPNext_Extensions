# Release 5.3.33 — Orphaned Queued recovery for Asset Depreciation Repair Campaign

## Summary

Fixes the Production feeder-dead state left after v5.3.32 Processing recovery:

**Queued Campaign Items whose parent RQ chunk job is gone** were still counted
as inflight, so `ensure_campaign_liveness` returned `enqueued_chunks=0` despite
Pending work and zero live RQ jobs.

**Scope:** queue/liveness only. No accounting-engine, JE cancel, ADS, IRR, or
index changes.

## Root cause

v5.3.32 recovered abandoned **Processing** only.

`ITEM_IN_FLIGHT = {Queued, Processing}` → 37 orphaned Queued rows across 9 dead
chunk_ids made `approx_chunks ≥ max_inflight_chunks=1`, blocking the feeder.

## Fix

1. `recover_orphaned_queued` / `_recover_orphaned_queued_internal`
   - Group Queued by `chunk_id`
   - Authoritative RQ state via `_chunk_rq_job_state`:
     `active` / `terminal` / `missing` / `unknown`
   - Recover only `missing`/`terminal` after 90s grace on `queued_at`
   - Skip `active` and `unknown`
   - `FOR UPDATE` + conditional `UPDATE … WHERE status='Queued' AND chunk_id=…`
2. Destination:
   - `attempt_count == 0` → **Pending** (repair never started this cycle)
   - `attempt_count > 0` → **Retryable Failed**
   - Clear `chunk_id`, `claim_token`, `queued_at`; set `error_type=ORPHANED_QUEUED`
   - **Does not** increment `attempt_count`
3. Liveness order: Processing recover → Queued recover → refresh counts → enqueue
4. Old-job safety:
   - `_claim_item` requires `status='Queued' AND chunk_id=<this chunk>`
   - `process_campaign_chunk` filters `item_names` to owned Queued+chunk

## Not changed

- `JournalEntry.cancel()` / Reset & Rebuild accounting
- Indexes from 5.3.32
- Chunk weighting defaults
- Depreciation scheduler behavior

## Tests

35 campaign unit tests PASS including Production-shape simulation
(37 Queued / 9 dead chunks / max_inflight=1) and claim/chunk mismatch safety.

Real RQ Dev smoke: orphan group recovered, live Queued preserved, workers
finished subsequent chunks, old-chunk duplicate claims = 0.

## Production deploy (after this release)

Campaign `AUD-ADRC-2026-00002` — Success 100 / Pending 2275 / Queued 37 orphaned /
Processing 0 / Retryable 23 / Failed 21; `max_inflight_chunks=1`.

1. Deploy **5.3.33** (no new migration/index required)
2. Restart workers / reload scheduler events
3. Keep `depreciation.post_depreciation_entries` **stopped=1**
4. Verify 0 live `Asset Depr Repair AUD-ADRC-2026-00002-*` RQ jobs
5. `ensure_campaign_liveness("AUD-ADRC-2026-00002")`
6. Expect ~37 Queued → Pending/Retryable; accounting writes from recovery = 0
7. Expect bounded enqueue (1 chunk)
8. Canary 10–20 NEW Success; require 0 timeout, 0 tabSeries 1020, G0–G12 PASS
9. Continue same campaign at `max_inflight_chunks=1`
10. Do not auto-retry historical Failed; do not re-enable depreciation posting
