# ERPNext Extensions v4.8.2

The Daily Production Log approach (4.7.2 – 4.8.1) is dropped. Only the bug fixes to the v16
semi-finished-goods Job Card flow that ERPNext 16.33 does **not** fix itself are kept.

## Removed

- Module **Daily Production**: DocTypes `Daily Production Log` / `Daily Production Log Document`,
  the runner, the REST e2e plan, the two setup patches, `docs/DAILY_PRODUCTION_LOG.md`,
  `RELEASE_4_8_1.md`.
- `stock_extensions/job_card_process_loss.py` (3.8.8) and its monkey-patch registration:
  ERPNext 16.33 (frappe/erpnext#58262) derives the Manufacture entry's process loss through
  `get_pending_process_loss_qty` — the Job Card's own unbooked loss for card entries, and only
  the not-yet-booked portion of the operation loss for Work Order level entries — which is the
  bug this override worked around, done properly.
- Patch `remove_daily_production_log` (post model sync, idempotent) deletes the two DocTypes and
  their tables, the *Daily Production* Module Def and the `Batch.custom_is_placeholder_lot`
  custom field from sites that had 4.7.2 – 4.8.1 installed. Job Cards, Stock Entries and
  Batches created through the runner are ordinary documents and stay.

## Kept — checked against ERPNext 16.33.0 source, still required

Moved unchanged from `daily_production/job_card_hooks.py` to
`stock_extensions/job_card_semi_fg.py` (same `hooks.doc_events["Job Card"]`):

| Hook | Bug | 16.33 |
| --- | --- | --- |
| `after_insert` — give every Job Card of a *Track Semi Finished Goods* Work Order `semi_fg_bom = Work Order.bom_no` | Cards created at Work Order submit get `semi_fg_bom` from the operation row's `bom_no` (empty), cards from the *Create Job Card* dialog get the parent BOM; the card's Manufacture entry carries `bom_no = semi_fg_bom` and `get_consumed_operating_cost` filters on it, so lots of the two kinds of card do not see each other and operating cost is booked twice | `create_job_card` / `get_operation_details` and `get_consumed_operating_cost` unchanged — still needed |
| `validate` — refuse a Pending Qty on a card that already has Completed Qty | `validate_job_card_qty` sums the raw `for_quantity` of every card of the operation, so a pending remainder counts as claimed and no further card can be created for it | `validate_job_card_qty` unchanged (only the *Create Job Card* button now discounts pending qty, #58262) — still needed |

## Migration

`bench migrate` runs the removal patch (drops the two Daily Production tables); then restart.
