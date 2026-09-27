# RELEASE 5.3.10 — Nested RIV engines resolve declared target item

## Problem
MAT-STE-2026-28696 EXACT repair stamped SLE at 346357, but SRC/TGT RIV Failed on
I3 for unrelated FG `20100064` on `MAT-STE-2026-24872-2`. Nested
`update_entries_after` for dependant FG warehouses often has no `repost_doc`, so
`get_riv_target_item_codes` returned `None` (fail-closed) and aborted the RIV.

## Fix
During `through_repost_item_valuation`, resolve declared targets from:
1. frozen `iran_riv_declared_target_items` cache (now also set for item_code RIVs);
2. active In-Progress RIV `item_code`.

Combined with 5.3.9 out-of-scope SLE restore, target RIV can Complete without
creating new I1 on unrelated FG rows.
