# RELEASE 5.3.12 — Leftover-MA post-RIV replay allows negative stock

## Bug
28696 SRC RIV Completed with I1=0. TGT RIV SLE/GL work succeeded, then
`leftover_ma.on_repost_item_valuation_update` → `replay_downstream_after_patient_zero`
called `update_entries_after(..., allow_negative_stock=False)`, which aborted the
Completed RIV with Insufficient Stock on unrelated FG dependant hops.

## Fix
Use `allow_negative_stock=True` for leftover-MA historical replay paths (matches
official RIV allow_negative_stock).
