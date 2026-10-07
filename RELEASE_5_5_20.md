# erpnext_extensions 5.5.20

## Iran Accounting — Preserve SAME_ITEM_MULTI_FG through RIV recalculate

### Summary

1. **Defect** — During Item+Warehouse RIV, `get_lazy_doc` reload drops in-memory
   closed-plan ownership stamps. Core `calculate_rate_and_amount` can then
   reintroduce multi-FG double-pool amounts; aligners may persist them before
   SLE sync. Observed on MAT-STE-2026-40687 Retain RIV (both MAIN_FG rows
   temporarily equalled the allocatable pool until mandatory T4 reassert).

2. **Fix** — `apply_irr_stock_entry_contract_after_calculate` re-asserts
   `SAME_ITEM_MULTI_FG` as the **last economic writer** after rate-first / FG
   residual aligners, gated by `detect_same_item_multi_fg_eligibility`. STAGE_CO,
   Product Reject, and By-Product paths are unchanged.

3. **Test** — `test_u21b_riv_last_writer_recovers_after_closed_stamp_loss`.

4. **Out of scope** — No general historical repair campaign. No ERPNext Core patch.

### Version

Patch bump from 5.5.19 → 5.5.20 (production RIV closed-plan protection).
