# RELEASE 5.3.7 — Wrong Rate provenance + Job Card Paykar + Bin pipeline

Development-only checkpoint on `development.localhost`.
Baseline dump: `20260924_103455-erp_espadpharmed_com-database.sql.gz`.
No push.

## Why

The MAT-STE-2026-33496 Wrong Rate canary was unsafe because expected=223282 was
**false EXACT**: `_outgoing_sle` matched empty `SLE.batch_no` and stole a sibling
detail's SVD. Official RIV then produced ~1.59M warehouse MA and inflated
Patient Zero / Wrong Rate READY. This release **stops that mutation path** and
continues cleaning via classifier + independent DERIVED_STATE.

## Code changes

1. **transfer_valuation** — detail-scoped SLE lookup; Material Issue zero no longer
   copies sibling SVD as EXACT; only true Material Transfer gets EXACT from
   outgoing SVD.
2. **wrong_rate** — Manufacture rows refuse generic `previous_healthy_sle` EXACT;
   require native manufacture valuation contract.
3. **job_card_flow** — shared Paykar Bin capped to JC transfer remainder
   (443 false MANUAL → OPEN_WITH_VALID_REMAINDER).
4. **repair_pipeline** — Bin drift uses `_bin_from_last_sle` (was calling
   `_update_bin` with wrong arity).

## Proven results (Development)

| Check | Result |
|---|---|
| 33496 classification | WAITING_UPSTREAM / not READY |
| Wrong Rate READY | 54 → **0** |
| Job Card MANUAL | 921 → **478** |
| Job Card OPEN_VALID | 393 → **836** |
| Job Card BROKEN | 89 (unchanged — true qty residuals) |
| Bin canary 30100293 | value 0→177; mismatches 1→0; I1=0; mfg Δ=0 |
| 16100066 LMA | already LEFTOVER_MA_REPAIRED; second repair sql_updates=0 |

## Not auto-repaired (documented)

- Manufacture WR: need native manufacture contract; generic rates blocked.
- Zero Rate (~488): mostly MTfM/Manufacture Z3/Z4 — provenance grouped, not invented.
- Posting Order actionable include Manufacture CROSS_TIME + WO rate rebuild — MANUAL until non-mfg chronology is proven.
- GL READY G1 on Manufacture voucher — deferred (stock/valuation unstable).
- Completed Job Card ± residuals (52+36) — quantity facts; read-only MANUAL.

## Tests

- `test_transfer_valuation_v530` (+ sibling / voucher_detail cases)
- `test_simple_historical_repair_v535` (+ shared Paykar, manufacture EXACT refusal)
