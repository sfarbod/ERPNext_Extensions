# erpnext_extensions 5.8.1

## ETO Subcontracting Receipt cost propagation on repost

### Problem

After an ETO Subcontracting Receipt (SCR) service cost is corrected, Repost Item
Valuation updates the SCR, but the corrected batch cost never reaches the
Material Transfer for Manufacture, the Manufacture consume row, the finished
good, or later issues. Staging reproduction (ERPNext 16.60.0, ext 5.8.0):

| Document | Expected | Observed on 5.8.0 |
|---|---|---|
| MAT-SCR-2026-00016 (61 × 30600001) | 135,211,380 | 135,211,380 ✅ |
| MAT-STE-2026-26587 transfer outward | 135,211,380 | 106,420,844 ❌ |
| MAT-STE-2026-26595-1 consume / FG 20100043 | ≈ 2.43M / box | 1,959,569 ❌ |

### Root cause

Core `update_entries_after.update_serial_batch_no_valuation` recomputes the
batch-wise outward rate (`BatchNoValuation`) and writes it to
`Serial and Batch Entry.incoming_rate` / `stock_value_difference`. It never
writes `outgoing_rate`.

The integer SABE policy (`apply_integer_sabe_policy_to_sle`) then chose the raw
rate as `outgoing_rate` for outward rows whenever it was set. Outward SABE rows
already carry an `outgoing_rate` stamped by an earlier integerization
(1,744,604 on the staging rows), so the policy wrote the **old** rate back over
Core's fresh one. The SLE, SED and FG then followed the stale SABE.

### Fix (scope: ETO only)

New `iran_accounting/domain/eto_scr_repost_scope.py`:

* `is_eto_repost_in_progress(engine)` is True only inside a Repost Item Valuation
  started for:
  * *Transaction* on a Subcontracting Receipt whose Subcontracting Order bills an
    ETO service item (default `23000001`), or
  * *Item and Warehouse* for the finished good of an ETO service item (`30600001`).
  All SLEs processed by that repost (transfer, Manufacture consume, FG, issues)
  are in scope.
* `select_sabe_raw_rate(row, prefer_core_outward_rate=...)`: in ETO scope, an
  outward row whose Core `incoming_rate` differs from the stamped `outgoing_rate`
  by ≥ 0.5 IRR uses Core's rate. Otherwise behaviour is unchanged.

`apply_integer_sabe_policy_to_sle` uses this helper. Reposts of any other item or
voucher keep the previous behaviour.

ETO service items can be overridden in `site_config.json`:

```json
{"iran_eto_service_items": ["23000001"]}
```

### Tests

`erpnext_extensions/iran_accounting/tests/test_eto_scr_repost_propagation.py`
(14 tests): rate selection in / out of ETO scope, sub-rial noise, inward rows,
SCR / item-warehouse / other-voucher scope resolution, site-config override.

### Acceptance on staging (to run after deploy)

1. RIV *Transaction* on MAT-SCR-2026-00016.
2. Expect MAT-STE-2026-26587 outward = −135,211,380, Manufacture
   MAT-STE-2026-26595-1 consume of 30600001 = −135,211,380, FG 20100043 revalued,
   issues MAT-STE-2026-42009 / 40099 revalued, GL balanced on every voucher.
