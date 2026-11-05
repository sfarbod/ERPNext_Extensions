# erpnext_extensions 5.7.1

## Manufacturing cost allocation — negative additional cost + illegal zero COMPONENT_SCRAP

### Issue 1 — Negative additional manufacturing cost SED/SLE drift

**Production exemplar:** `MAT-STE-2026-43589`

**Root cause**

1. `_spread_operating_cost` treated any non-positive `operating_total` as
   “clear all row `additional_cost`”. Negative header Additional Costs were
   wiped from SED while Core SLE/GL already absorbed them into FG stock value.
2. Unstamped submitted Manufactures never re-entered the full v5.3.3+ contract
   (`uses_v533_contract` False), so stale SED amounts were left untouched.
3. `repair_manufacture_selected` treated `sle_se_drift` alone as authority to
   run SLE←SED sync (`source_of_truth=5.3.0_se_to_sle_sync`), which would
   overwrite healthy SLE/GL from inflated SED.

**Fix**

- Negative header operating lands on FG SED (`additional_cost` / `amount` /
  `valuation_rate`); positive per-unit spread unchanged.
- Historical repair: `repair_historical_negative_additional_cost` for
  unstamped submitted docs.
- Evidence-based `classify_manufacture_sed_sle_authority`; when SLE is
  economically authoritative, set `refuse_sle_from_sed_sync` and never call
  `sync_sle_from_stock_entry_detail`.

### Issue 2 — SFG COMPONENT_SCRAP illegal zero

**Production exemplar:** `MAT-STE-2026-36826` item `30300051`

**Root cause**

Unstamped submitted Manufactures only repaired illegal-zero
`MAIN_PRODUCT_REJECT` / reject-only. Zero `COMPONENT_SCRAP` with
`allow_zero_valuation_rate=0` and a valid issued rate was left at zero;
preview/iran-native reported `ALREADY_HEALTHY`.

(`scrap_valuation_role=INPUT_MATERIAL_SCRAP` is the historical-stock diagnostic
label for “retain source valuation”; `classify_manufacture_outputs` already
places the same row in `COMPONENT_SCRAP`. The defect was missing historical
repair, not family inference.)

**Fix**

- `historical_zero_component_scrap_needs_repair` /
  `repair_historical_zero_component_scrap` price issued rate and restore FG
  residual.
- Wired into `apply_iran_manufacture_output_contract` for unstamped submitted
  docs (alongside negative-addl repair).
- Intentional bulk scrap (`allow_zero` / Manual zero) unchanged.

### Tests

`test_mfg_neg_addl_component_scrap_v571.py` — 12 tests (negative addl spread,
historical SED repair, SLE-authoritative refuse sync, COMPONENT_SCRAP illegal
zero, bulk scrap preserved, idempotency, positive addl unchanged).

Regression OK: reject-only v570/v571, component scrap issued rate, scrap
absorbed costing, bulk scrap, CASE F 27800, historical repair v530,
manufacture IRR residual.

### Production repair readiness (do not execute in this release task)

| Voucher | SED action | SLE action | RIV identities |
|---|---|---|---|
| MAT-STE-2026-43589 | Persist FG amount/rate with negative header addl | **Do not** SLE←SED if SLE already matches consume+addl; normalize txn rates from SVD only | After SED catch-up: `30300025` Quarantine + Reject (only if SED write requires identity refresh) |
| MAT-STE-2026-36826 | Price `30300051` scrap at issued rate; reduce FG residual | Sync SLE from corrected SED for scrap + FG | `30300051` @ Reject WH; `20100008` @ Quarantine FG |

APIs: `repair_manufacture_selected_api` (dry_run then apply). Item+Warehouse RIV
via `repost_selected_api` for affected identities only.

### Unsupported / unchanged by design

- Multi-MAIN_FG + negative additional cost: historical negative-addl repair
  requires exactly one FG row (same multi-FG fail-closed policy as absorbed cost).
- Zero issued rate / unmatched scrap item: no forced COMPONENT_SCRAP rate.
- Positive additional cost Main+Reject per-unit spread unchanged.
- Reject-only v5.7.0 path unchanged.
- Bulk scrap intentional zero unchanged.
- Co-product / STAGE_CO unchanged.
