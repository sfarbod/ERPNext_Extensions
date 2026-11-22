# Quarantine: MCA-2026-00004 / MAT-STE-2026-25912

**Classification:** quarantined accounting exception (operational exclusion).  
**Date:** 2026-11-21 (Development)  
**Evidence:** `/tmp/mca_00004_readonly_investigation.json`, `/tmp/mca_pause_checkpoint.json`

## Status (do not mutate)

| Field | Value |
|---|---|
| MCA | `MCA-2026-00004` |
| Stock Entry | `MAT-STE-2026-25912` (Submitted) |
| `processing_status` | `Allocating` |
| `checkpoint` | `allocated` |
| `repost_status` | `Failed` |
| RIV | none |
| Redis SE lock | absent (orphan released at pause) |

## Why quarantined

Interrupted mid `Rebuilding SLE` (regression suite stop). Split-brain:

- Additional Costs applied **exactly once** (2 rows, sum = MCA total).
- SED FG reflects MCA (`additional_cost` / high `valuation_rate`).
- SLE / SABE / GL **do not** reflect MCA cost accounts (GL balanced at pre-MCA FG value; 211237/211238 absent).

Blind resume risks duplicate costs or incomplete remirror. Mass processing must skip this name.

## Code exclusion

`constants.QUARANTINED_MCA_NAMES` includes `MCA-2026-00004`.

Skipped by:

- `process_mca` → `{ok: False, reason: "quarantined"}`
- `process_pending_mca_reposts`
- `on_riv_change` enqueue

Document rows are **not** deleted, cancelled, or amended.

## Also excluded (permanent business failures)

Negative `pool_value` Failed set (markers in `PERMANENT_ERROR_MARKERS`):  
`MCA-2026-00001`, `00003`, `00005`, `00017`, `00023`.

## Controlled recovery sequence (authorization required)

Do **not** run until explicitly authorized. Do **not** re-append costs or create RIV first.

1. Confirm quarantine still listed; take a fresh read-only snapshot of SE/SLE/SABE/GL/costs.
2. Ensure Iran bootstrap + `max_writes_per_transaction` ≥ 5e6 on the runner.
3. From checkpoint `allocated` only: run SLE refresh → Iran remirror/integerize → source GL refresh → ledger contract (single controlled `process_mca` or equivalent engine steps).
4. Verify FG: SED.amount ≈ SLE.SVD; SABE.incoming_rate integer IRR; MCA expense accounts appear in GL; GL balanced.
5. Only then create/enqueue Transaction RIV on queue `long`; wait for Completed.
6. Finalize MCA → `Completed` / checkpoint `completed`.
7. Idempotent re-entry must return `already_terminal` with unchanged cost/GL counts.
8. Remove name from `QUARANTINED_MCA_NAMES` only after sign-off.

If contract still fails after step 3–4: stop; use compensate/restore under a new written plan — still no mass retry.

## Mass-run filter

Any future batch must exclude:

```text
QUARANTINED_MCA_NAMES ∪ {MCA with last_repost_error containing "negative pool_value"}
```
