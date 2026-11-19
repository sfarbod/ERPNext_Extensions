# Investigation: ETO Subcontracting Receipt cost does not propagate on repost

Status: **root cause found — fixed in 5.8.1 (see RELEASE_5_8_1.md), pending staging acceptance**
Scope: ETO outsourcing (service item `23000001` "برون سپاری ETO", FG `30600001`)
Observed on: erpnext 16.53.0, erpnext_extensions 5.7.26 (staging) and 5.7.56 (production)

## Business context

ETO sterilisation is billed per carton under a contract tier (Article 4), not per
syringe. 83 ETO Purchase Orders / Subcontracting Orders were repriced to
`cartons × per-carton rate`. The Subcontracting Receipts (SCR) still carry the old
per-syringe service cost, so stock of `30600001` and everything made from it
(finished product, issues) is mis-valued.

Required behaviour: after the SCR service cost is corrected, a Repost Item
Valuation must carry the new `30600001` batch cost through:

1. Material Transfer for Manufacture (Quarantine → WIP)
2. Manufacture (`30600001` consumed → finished product)
3. Later Material Issues of the finished product

## Reproduction (staging, one product / one receipt)

| Step | Document | Expected | Observed |
|---|---|---|---|
| SCR service cost set from PO (36,280,520 / 61) | MAT-SCR-2026-00016 | rate 2,216,580 | ✅ SLE + SABE inward 61 × 2,216,580 = 135,211,380; GL balanced (rounding to 621301) |
| Transfer Quarantine → WIP, batch `504184-30600001-so252039t112` | MAT-STE-2026-26587 | outward 61 × 2,216,580 | ❌ stays 61 × 1,744,604 = 106,420,844 |
| Manufacture consume + FG `20100043` | MAT-STE-2026-26595-1 | FG ≈ 2.43M / box | ❌ FG stays 1,959,569 |
| Issues of FG batch | MAT-STE-2026-42009, MAT-STE-2026-40099 | revalued | ❌ unchanged |

Reposts tried (all `Completed`, none changed the downstream values):

1. RIV *Transaction* on the SCR.
2. RIV *Item and Warehouse* — `30600001` @ `انبار Quarantine محصول نیمه ساخته اسپاد` from 2026-04-20 18:06:35 (single clean run, no timeout).
3. Manually set SED of the transfer row (`hta77cg137`) and the Manufacture consume row
   (`ir33jp73ev`) to 2,216,580 / 135,211,380, then RIV *Transaction* on the transfer.
   The repost **rewrote both SED rows back to 1,744,604 / 106,420,844**.

## Facts established

* Batch has `use_batchwise_valuation = 1`; item is not serialised; no Stock Closing Entry exists.
* Raw `Serial and Batch Entry` rows for the batch in Quarantine before the transfer:
  only the SCR inward (+61, 135,211,380). Core `BatchNoValuation.get_batch_stock_before_date`
  applied to this data yields 2,216,580.
* Core `update_serial_batch_no_valuation` only skips the SABE write when the computed
  rate equals the stored one — the transfer's outward bundle (`831671bb1742902dc4f3`)
  was **not** rewritten (modified time unchanged), i.e. the effective computed rate stayed
  1,744,604, or Core was not reached for this SLE.
* `Material Transfer for Manufacture` is in `NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES`, so
  `sync_irr_sle_from_stock_entry_row` preserves the vanilla movement and realigns SED from
  it (`_preserve_native_outgoing_and_align_sed`). That explains why the manual SED edit was
  undone — the vanilla movement itself was still 106,420,844.

## Suspected areas (not yet confirmed)

The `process_sle` wrapper in `iran_accounting/integration/monkey_patches.py`:

* `assert_sle_valuation_integrity_before_vanilla` → out-of-scope soft-skip
  (`restore_out_of_scope_sle_ledger_state`) — would leave the historical SLE untouched.
* `reseed_wh_data_from_chronological_previous` / `clear_sticky_native_outgoing_rate_for_riv`.
* `apply_integer_sabe_policy_to_sle` (integer SABE policy, `align_sle`) and
  `assert_sle_valuation_integrity_after_sync` → `restore_out_of_scope_sle_ledger_state`.
* The `update_serial_batch_no_valuation` wrapper (`_iran_sabe_irr_integer_guard`).

Suggested debug: enable logging around each early-return in the wrapper and run
RIV *Item and Warehouse* for `30600001` @ Quarantine from 2026-04-20 18:06:35 on staging;
check which branch handles SLE of `MAT-STE-2026-26587` (outward leg).

## Requested fix (scope: ETO only)

For outward legs of **Material Transfer for Manufacture** and consume rows of
**Manufacture** whose batch was created by a Subcontracting Receipt for service item
`23000001` (batch `reference_doctype = "Subcontracting Receipt"`), a repost must use the
Core batch-wise rate derived from SABE history and propagate it (SLE, SABE, SED, FG
recalculation, downstream issues). Behaviour for all other items must stay unchanged.

Acceptance test: the staging reproduction above ends with transfer = 135,211,380,
FG `20100043` ≈ 2,432,498 per box (integer composition per IRR contract), issues revalued,
GL balanced on every voucher.

## Related operational notes

* Repost workers time out after 300 s (`rq.timeouts.JobTimeoutException`); long reposts
  resume over multiple runs. Production has a backlog of ~3,800 RIVs.
* Landed Cost Vouchers on SCR (production MAT-LCV-2026-00010…00051) show the same
  non-propagation: the added cost stays in the Quarantine warehouse value pool.

## Root cause (5.8.1)

Outward Serial and Batch Entry rows of the transfer and Manufacture carry a stale
`outgoing_rate` = 1,744,604. Core writes the fresh batch rate only to
`incoming_rate` / `stock_value_difference`; `apply_integer_sabe_policy_to_sle`
preferred `outgoing_rate` for outward rows and rewrote the old rate. 5.8.1 makes
the policy use Core's rate for outward rows during ETO-scoped reposts.

## Additional root cause (5.8.3) — empty-warehouse MA residual

For batches that empty Quarantine (MAT-SCR-2026-00011 / MAT-STE-2026-26674),
Iran consume-to-zero policy kept warehouse MA residual as SLE authority and
refused to stamp batch SABE onto SLE. SABE stayed at the SCR batch rate while
SLE/SED absorbed the understated warehouse layer (e.g. 352,317 vs 583,416).

5.8.3: during ETO-scoped repost, `should_align_empty_warehouse_sle_from_batch_sabe`
aligns SLE/SED from batch SABE for ETO finished goods emptying a warehouse.

## Orchestration API (5.8.3)

`erpnext_extensions.iran_accounting.eto_scr_valuation_propagation`:

- `inspect_scr_eto_valuation_chain` / `plan_scr_eto_valuation_propagation`
- `propagate_scr_eto_valuation` (confirm token; optional service-cost phase;
  supported stock RIV only; no LCV recreation)

## Development acceptance (2026-11-20)

| SCR | Result |
|---|---|
| MAT-SCR-2026-00016 | Service cost corrected; SLE/Transfer/Manufacture 2,216,580; FG ≈ 2,431,545 |
| MAT-SCR-2026-00011 | Service cost corrected; empty-warehouse drift cleared; chain @ 536,809 |
| MAT-SCR-2026-00023 | LCV deleted; chain consistent @ 607,745; LCV not recreated |
