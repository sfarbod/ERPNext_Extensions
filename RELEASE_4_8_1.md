# ERPNext Extensions v4.8.1

Daily Production Log — fixes found while running the end-to-end plan on staging after 4.7.4.

## Fix — false "running timer" on any draft Job Card listing the operator

- The operator guard queried `tabJob Card Time Log` for rows without `to_time`. Job Card's
  **Employee** multi-select stores its rows in the same child table (`parentfield = employee`,
  `from_time` / `to_time` always empty), so any **draft** card that merely listed the operator —
  typically a card whose job was completed but not yet submitted — blocked every new cycle with
  *"Employee … still has a running timer on Job Card …"*.
- The guard now only looks at `time_logs` rows.

## Fix — previous-operation availability for batch/serial-tracked inputs

- When the semi-finished input is batch/serial tracked but this Work Order has produced no lot
  yet, the check fell back to the plain Bin quantity, which can hold **another** Work Order's
  lot; the cycle then failed later on core's operation-sequence rule instead of the clear
  *"Only 0 of ITEM (output of the previous operation) is available"* message.
- Tracked inputs now count only this Work Order's lots, even when that is zero. Untracked
  inputs still use the Bin quantity.

## Fix — placeholder lot counter restarts after a manually named batch

- The site-wide numeric prefix of `<counter>-<item>-<lot>` was derived from the **most recent**
  batch id; a manually named batch in between (`DPL-…-MAN`) restarted it at 1 (ids stayed
  unique through the exists-loop, but the sequence was no longer monotonic).
- The prefix is now `max(numeric prefix in use) + 1`.

## E2E test plan (`daily_production/e2e/run_test_plan.py`)

- Re-runnable on a site that still holds earlier runs: the five test days are chosen
  automatically as the day after the operators' last time log (core refuses overlapping
  employee time logs); `--start-day` overrides.
- The manually driven op-3 card now gets its Material Transfer through the desk mapper
  (`transfer_for_card`) before submit — core rejects submitting a card without transfer.
- `DPL_MAX_SECONDS` (default 10) sets the per-cycle time budget.

## Staging validation (erpstage, 2026-08-27, app 4.7.4 + this plan)

`MFG-WO-2026-00441` (BOM-20100067-017, 2756 units) ran end to end through Daily Production
Logs only: Work Order **Completed / 2756 produced**, **12 Job Cards**, every card
`for_quantity == completed`, `semi_fg_bom` on all cards, booked operating cost == Work Order
actual on all five operations; all failure cases, the concurrency test, the re-run of a Failed
log and idempotency passed. Op 5 (packaging, 21 materials) takes 11–12.5 s per cycle against
the 10 s budget; all other cycles 4–10 s.

Data finding (not code): the BOM sources packaging items `13200412` and `13200317` from the
**primary** packaging store while stock is held in the **secondary** store. The runner fails
closed on this (card warehouse → BOM warehouse only); for the test 500 / 300 units were moved
to the primary store (`MAT-STE-2026-08762`). Prod needs either the BOM source warehouse or the
stock location corrected before packaging cycles can run.

## Unchanged

- No schema change; no patch. `bench migrate` + restart is enough.
