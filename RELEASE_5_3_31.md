# Release 5.3.31 — Asset Depreciation Reset & Rebuild background campaign

## Summary

Adds **queue/campaign orchestration** for Asset Depreciation Reset & Rebuild.

**No accounting repair semantic change.** The authoritative per-Asset operation
remains:

`erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild.reset_and_rebuild_asset`

(v5.3.29 engine, preserved through v5.3.30).

## Why

Synchronous HTTP/API execution of thousands of Assets (~140k Depreciation Entry
JEs on Production scale) is too slow and hits gateway timeouts. Production needs
a resumable Frappe background campaign that:

- returns immediately from the API
- processes Assets on the `long` queue in bounded chunks
- commits **one Asset at a time**
- never redoes already-successful repairs
- retains MANUAL_REVIEW / BLOCKED / FAILED in an exception register

## Added

| Piece | Role |
|-------|------|
| DocType **Asset Depreciation Repair Campaign** | Campaign master + counters |
| DocType **Asset Depreciation Repair Campaign Item** | Per-Asset claimable state |
| `services/depr_reset_rebuild_campaign.py` | Whitelisted campaign API + worker |
| Posting-guard campaign claim check | Skip nightly posting while Asset is Queued/Processing in a Running campaign |

### Whitelisted APIs

- `create_campaign`
- `analyze_campaign`
- `start_campaign` (optional `run_inline=1` for tests/dev without workers)
- `pause_campaign` / `resume_campaign` / `stop_campaign`
- `get_campaign_status`
- `get_campaign_assets`
- `get_campaign_exceptions`
- `export_campaign_exceptions_csv`
- `enqueue_next_chunks`

Worker entry `process_campaign_chunk` is **not** whitelisted.

### Defaults

- Queue: `long`
- Chunk size: `20` (clamped 10–25)
- Max inflight chunks: `4`
- Max attempts (transient only): `3`

### Exception register

All Manual Review / Blocked / Failed items are persisted with reason codes and
specialist recommendations; CSV export available via API.

## Not changed

- JE identity / `JournalEntry.cancel()`
- VAD reconciliation
- ADS rebuild + Business Calendar
- Iran whole-IRR + life-final balancing
- G0–G12 gates
- Posting idempotency guard core behavior
- No `make_depreciation_entry` / no rebook

## Production deployment requirements

1. Deploy **5.3.31** to Production and migrate DocTypes.
2. Ensure `long` queue workers are running.
3. Keep `depreciation.post_depreciation_entries` stopped (or rely on campaign
   claim guard) during the repair wave.
4. Create campaign from remaining READY Assets (skip the ~408 already SUCCESS).
5. Queue canary → scale to 2 workers → full wave.
6. Deliver exception register to specialists.

## Tests

- `test_depr_reset_rebuild_campaign` (helpers, DocTypes, worker inline mocks)
- Development canary: already-repaired skip, Usage MANUAL_REVIEW, Scrapped BLOCKED,
  dual-claim, exception CSV
- Existing ADS normalize / Mode B / accounting amount regression suite
