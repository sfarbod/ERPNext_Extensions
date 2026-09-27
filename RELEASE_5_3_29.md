# Release 5.3.29 — Asset Depreciation whole-IRR ADS + Reset & Rebuild

## Problem

IRR Asset Depreciation Schedules persisted fractional `depreciation_amount`
values while Depreciation Entry JEs post at whole IRR. Failed ADS↔JE amount
matching left `journal_entry` unlinked, so the nightly scheduler re-posted the
same period every day (e.g. Asset 3760: 39 submitted JEs for ~3 due periods).

## Fix

1. **Authoritative ADS normalization** (`ads_amount_normalize`)
   - Every IRR ADS `depreciation_amount` is whole via `to_depr_amount` /
     `round_currency(..., 0)` (never Python `round()`).
   - Cumulative rounding residual is absorbed **only** by the life-final
     installment; that final amount is also whole IRR.
   - Wired as the last monetary gate on ADS `before_validate` / `before_submit`,
     and reused by Asset Usage replan (Mode A/B) and Reset & Rebuild.

2. **Depreciation posting idempotency guard** (`depr_posting_guard`)
   - Before creating a Depreciation JE: skip if row already linked; else find
     existing submitted JE by Asset + schedule_date; link if exactly one;
     raise on multiples; create only if none.
   - Installed via Iran bootstrap + AUD `after_migrate`.

3. **Asset Depreciation Reset & Rebuild** (`depr_reset_rebuild`)
   - APIs: `analyze_asset`, `reset_and_rebuild_asset`, `reset_and_rebuild_batch`
     (whitelist; dry-run optional, not required before Apply).
   - Lifecycle: lock → fresh preflight → `JournalEntry.cancel()` of identity-
     matched Depreciation Entries → explicit Asset VAD / booked-depr
     reconciliation → rebuild via ERPNext `create_depreciation_schedule`
     (Business Calendar dates preserved) → Iran normalize/balance → replace
     Active ADS → G0–G12 gates → commit.
   - **Does not** create Depreciation Entry JEs; nightly scheduler posts due rows.
   - Blocks Sold/Scrapped/Cancelled/disposal; MANUAL_REVIEW for Usage periods,
     Asset Value Adjustment, capitalized Asset Repair, multi Active ADS, etc.

## Development canaries (development.localhost)

| Asset | Scenario | Before JEs | After reset | ADS rows | Frac after | Due | 1st post | Dup after repeats |
|-------|----------|------------|-------------|----------|------------|-----|----------|-------------------|
| 3760 | daily prorata canary | 39→0* | 0 | 37 | 0 | 3 | 3 | 0 (40 runs) |
| 3752 | daily_prorata=0 | 35 | 0 | 61 | 0 | 3 | 3 | 0 |
| 1931 | daily prorata | 10 | 0 | 9 | 0 | 6 | 6 | 0 |
| 1951 | daily prorata | 10 | 0 | 9 | 0 | 6 | 6 | 0 |
| 3660 | many duplicates | 65 | 0 | 97 | 0 | 5 | 5 | 0 |

\*3760 had already been rebuilt in an earlier canary; final acceptance re-posted
with explicit commit: due amounts `13,413,699 / 34,652,055 / 34,652,055`,
final installment `21,238,347`, 3 JEs after first post, 0 duplicates after 40 runs.

Blocked/MANUAL: Scrapped Asset 3455 → `BLOCKED`; Usage Assets 3873/3874 →
`MANUAL_REVIEW`.

## Tests

- `test_ads_amount_normalize` — positive/negative residual; final row whole IRR
- `test_depr_reset_rebuild` — site canary helper + blocked lifecycle
- AUD regression: `test_accounting_amounts`, `test_mode_b` (+ normalize)

## Scope / not in this release

- No Production modify or push
- No tool-side rebook of due depreciation JEs
- Asset Usage bulk APPLY remains MANUAL_REVIEW (future ADS rounding is compatible)
